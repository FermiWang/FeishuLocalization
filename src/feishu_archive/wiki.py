from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    MAX_SINGLE_ATTACHMENT_BYTES,
    ArchivePaths,
    resolve_archive_resource_path,
)
from .database import ArchiveDatabase


BLOCK_NAMES = {
    1: "page",
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    9: "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
    19: "callout",
    22: "divider",
    23: "file",
    27: "image",
    32: "table_cell",
    34: "quote_container",
}
DOCX_TYPES = {"docx"}
SUPPORTED_METADATA_ONLY_TYPES = {
    "doc",
    "sheet",
    "bitable",
    "mindnote",
    "slides",
    "board",
    "file",
}
WIKI_RENDER_VERSION = "4"


@dataclass
class WikiSyncCounts:
    spaces_seen: int = 0
    nodes_seen: int = 0
    documents_seen: int = 0
    documents_written: int = 0
    assets_downloaded: int = 0
    assets_skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "spaces_seen": self.spaces_seen,
            "nodes_seen": self.nodes_seen,
            "documents_seen": self.documents_seen,
            "documents_written": self.documents_written,
            "assets_downloaded": self.assets_downloaded,
            "assets_skipped": self.assets_skipped,
        }


class WikiSyncer:
    def __init__(
        self,
        database: ArchiveDatabase,
        client: Any,
        paths: ArchivePaths,
        *,
        max_asset_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_single_asset_bytes: int = MAX_SINGLE_ATTACHMENT_BYTES,
    ) -> None:
        self.database = database
        self.client = client
        self.paths = paths
        self.max_asset_bytes = max_asset_bytes
        self.max_single_asset_bytes = max_single_asset_bytes
        self.paths.ensure()

    def discover_spaces(self) -> list[dict[str, Any]]:
        seen_at = int(time.time() * 1000)
        spaces: list[dict[str, Any]] = []
        for page in self.client.iter_wiki_space_pages():
            for item in page.get("items") or []:
                if not isinstance(item, dict) or not item.get("space_id"):
                    continue
                self.database.upsert_wiki_space(item, seen_at=seen_at)
                spaces.append(item)
        self.database.mark_unseen_wiki_spaces(seen_at)
        return spaces

    def sync(
        self,
        space_ids: list[str] | None = None,
        *,
        force: bool = False,
    ) -> tuple[WikiSyncCounts, list[str]]:
        counts = WikiSyncCounts()
        errors: list[str] = []
        discovered = self.discover_spaces()
        visible_ids = [str(item["space_id"]) for item in discovered]
        requested = space_ids or visible_ids
        if space_ids:
            unknown = [space_id for space_id in space_ids if space_id not in visible_ids]
            if unknown:
                errors.append("不可见或不存在的知识空间：" + ", ".join(unknown))
        for space_id in requested:
            if space_id not in visible_ids:
                continue
            counts.spaces_seen += 1
            try:
                self._sync_space(space_id, counts, force=force)
                self.database.update_wiki_space_sync(space_id, status="active")
            except Exception as exc:
                message = f"空间 {space_id}：{exc}"
                errors.append(message)
                self.database.update_wiki_space_sync(
                    space_id,
                    status="error",
                    error=str(exc),
                )
        return counts, errors

    def _sync_space(
        self,
        space_id: str,
        counts: WikiSyncCounts,
        *,
        force: bool,
    ) -> None:
        seen_at = int(time.time() * 1000)
        queue: list[tuple[str | None, str]] = [(None, "")]
        queued: set[str] = set()
        document_errors: list[str] = []
        while queue:
            parent_token, parent_path = queue.pop(0)
            position = 0
            for page in self.client.iter_wiki_node_pages(
                space_id,
                parent_node_token=parent_token,
            ):
                for item in page.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    node_token = str(item.get("node_token") or "").strip()
                    obj_token = str(item.get("obj_token") or "").strip()
                    if not node_token or not obj_token:
                        continue
                    title = str(item.get("title") or obj_token)
                    node_path = f"{parent_path}/{title}" if parent_path else title
                    self.database.upsert_wiki_node(
                        item,
                        space_id=space_id,
                        parent_node_token=parent_token,
                        path=node_path,
                        position=position,
                        seen_at=seen_at,
                    )
                    position += 1
                    counts.nodes_seen += 1
                    counts.documents_seen += 1
                    try:
                        changed = self._sync_node_document(item, force=force, counts=counts)
                        if changed:
                            counts.documents_written += 1
                    except Exception as exc:
                        self._record_document_error(item, exc)
                        document_errors.append(f"{title}：{exc}")
                    if item.get("has_child") and node_token not in queued:
                        queued.add(node_token)
                        queue.append((node_token, node_path))
        self.database.mark_unseen_wiki_nodes(space_id, seen_at)
        if document_errors:
            preview = "；".join(document_errors[:5])
            if len(document_errors) > 5:
                preview += f"；另有 {len(document_errors) - 5} 项失败"
            raise RuntimeError(preview)

    def _sync_node_document(
        self,
        node: dict[str, Any],
        *,
        force: bool,
        counts: WikiSyncCounts,
    ) -> bool:
        obj_token = str(node["obj_token"])
        obj_type = str(node.get("obj_type") or "unknown").lower()
        source_edit_time = _optional_int(node.get("obj_edit_time"))
        existing = self.database.get_wiki_document(obj_token)
        if not force and _is_current(existing, source_edit_time):
            return False
        if obj_type in DOCX_TYPES:
            return self._sync_docx(node, counts)
        if obj_type == "file":
            return self._sync_file(node, counts)
        status = "metadata_only"
        note = f"{obj_type} 当前保存目录元数据；正文离线化将在后续适配"
        if obj_type not in SUPPORTED_METADATA_ONLY_TYPES:
            note = f"尚未适配 {obj_type} 正文，已保存目录元数据"
        content_hash = _sha256_text(
            json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        return self.database.upsert_wiki_document(
            {
                "obj_token": obj_token,
                "obj_type": obj_type,
                "title": node.get("title") or "",
                "source_edit_time": source_edit_time,
                "content_text": note,
                "rendered_html": f'<p class="kb-note">{html.escape(note)}</p>',
                "content_sha256": content_hash,
                "status": status,
                "last_synced_at": int(time.time() * 1000),
                "raw_json": node,
            }
        )

    def _sync_docx(self, node: dict[str, Any], counts: WikiSyncCounts) -> bool:
        obj_token = str(node["obj_token"])
        metadata = self.client.get_docx_document(obj_token)
        blocks: list[dict[str, Any]] = []
        for page in self.client.iter_docx_block_pages(obj_token):
            blocks.extend(item for item in (page.get("items") or []) if isinstance(item, dict))
        raw_content = self.client.get_docx_raw_content(obj_token)
        title = str(metadata.get("title") or node.get("title") or "")
        source_edit_time = (
            _optional_int(node.get("obj_edit_time"))
            or _optional_int(metadata.get("update_time"))
            or _optional_int(metadata.get("edited_time"))
        )
        revision_id = _optional_int(metadata.get("revision_id"))
        content_hash = _sha256_text(
            json.dumps(blocks, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            + raw_content
        )
        # The provisional row owns the asset foreign keys while downloads run.
        self.database.upsert_wiki_document(
            {
                "obj_token": obj_token,
                "obj_type": "docx",
                "title": title,
                "revision_id": revision_id,
                "source_edit_time": source_edit_time,
                "content_text": raw_content,
                "rendered_html": "",
                "content_sha256": content_hash,
                "status": "syncing",
                "last_synced_at": int(time.time() * 1000),
                "raw_json": metadata,
            }
        )
        asset_ids: dict[tuple[str, str], int] = {}
        active_assets: set[tuple[str, str]] = set()
        for asset in extract_assets(blocks):
            key = (asset["file_token"], asset["asset_type"])
            active_assets.add(key)
            asset_id, downloaded = self._download_asset(
                obj_token,
                file_token=asset["file_token"],
                asset_type=asset["asset_type"],
                block_id=asset.get("block_id"),
                filename=asset.get("filename"),
                opener=self.client.open_drive_media,
            )
            asset_ids[key] = asset_id
            if downloaded:
                counts.assets_downloaded += 1
            else:
                counts.assets_skipped += 1
        self.database.prune_wiki_assets(obj_token, active_assets)
        normalized_blocks = [
            {
                "block_id": block.get("block_id"),
                "parent_id": block.get("parent_id"),
                "block_type": block.get("block_type"),
                "text": block_text(block),
                "raw_json": block,
            }
            for block in blocks
        ]
        asset_states = {
            key: self.database.get_wiki_asset(asset_id) or {}
            for key, asset_id in asset_ids.items()
        }
        rendered = render_blocks(blocks, asset_ids, asset_states)
        export_path = self._write_export(obj_token, title, rendered)
        changed = self.database.upsert_wiki_document(
            {
                "obj_token": obj_token,
                "obj_type": "docx",
                "title": title,
                "revision_id": revision_id,
                "source_edit_time": source_edit_time,
                "content_text": raw_content or "\n".join(block_text(item) for item in blocks),
                "rendered_html": rendered,
                "content_sha256": content_hash,
                "local_export_path": str(export_path.relative_to(self.paths.root)),
                "status": "synced",
                "last_synced_at": int(time.time() * 1000),
                "raw_json": metadata,
            }
        )
        self.database.replace_wiki_blocks(obj_token, normalized_blocks)
        return changed

    def _sync_file(self, node: dict[str, Any], counts: WikiSyncCounts) -> bool:
        obj_token = str(node["obj_token"])
        title = str(node.get("title") or obj_token)
        source_edit_time = _optional_int(node.get("obj_edit_time"))
        content_hash = _sha256_text(
            json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        self.database.upsert_wiki_document(
            {
                "obj_token": obj_token,
                "obj_type": "file",
                "title": title,
                "source_edit_time": source_edit_time,
                "content_sha256": content_hash,
                "status": "syncing",
                "last_synced_at": int(time.time() * 1000),
                "raw_json": node,
            }
        )
        asset_id, downloaded = self._download_asset(
            obj_token,
            file_token=obj_token,
            asset_type="file",
            filename=title,
            opener=self.client.open_drive_file,
        )
        if downloaded:
            counts.assets_downloaded += 1
        else:
            counts.assets_skipped += 1
        asset = self.database.get_wiki_asset(asset_id) or {}
        downloaded_ok = asset.get("status") == "downloaded"
        rendered = (
            f'<p><a class="kb-file" href="/api/wiki/preview/{asset_id}" '
            f'target="_blank" rel="noopener">{html.escape(title)}</a></p>'
            if downloaded_ok
            else '<p class="kb-note">文件元数据已保存，但文件内容尚未下载。</p>'
        )
        self.database.prune_wiki_assets(obj_token, {(obj_token, "file")})
        return self.database.upsert_wiki_document(
            {
                "obj_token": obj_token,
                "obj_type": "file",
                "title": title,
                "source_edit_time": source_edit_time,
                "content_text": title,
                "rendered_html": rendered,
                "content_sha256": content_hash,
                "status": "synced" if downloaded_ok else "metadata_only",
                "error": asset.get("error"),
                "last_synced_at": int(time.time() * 1000),
                "raw_json": node,
            }
        )

    def rebuild_views(self, *, force: bool = False) -> dict[str, int | str]:
        current_version = self.database.get_metadata("wiki_render_version")
        if current_version == WIKI_RENDER_VERSION and not force:
            return {
                "documents_seen": 0,
                "documents_updated": 0,
                "documents_skipped": 0,
                "render_version": WIKI_RENDER_VERSION,
            }
        seen = 0
        updated = 0
        skipped = 0
        for document in self.database.list_wiki_documents_for_render():
            obj_token = str(document["obj_token"])
            obj_type = str(document.get("obj_type") or "unknown").lower()
            title = str(document.get("title") or obj_token)
            assets = self.database.list_wiki_assets(obj_token)
            asset_ids = {
                (str(asset["file_token"]), str(asset["asset_type"])): int(asset["id"])
                for asset in assets
            }
            asset_states = {
                (str(asset["file_token"]), str(asset["asset_type"])): asset
                for asset in assets
            }
            if obj_type == "docx":
                blocks = self.database.list_wiki_blocks(obj_token)
                if not blocks:
                    skipped += 1
                    continue
                rendered = render_blocks(blocks, asset_ids, asset_states)
            elif obj_type == "file":
                asset = next(
                    (item for item in assets if item.get("asset_type") == "file"),
                    None,
                )
                rendered = render_file_asset(title, asset)
            else:
                skipped += 1
                continue
            seen += 1
            export_path = self._write_export(obj_token, title, rendered)
            if self.database.update_wiki_rendered_view(
                obj_token,
                rendered,
                local_export_path=str(export_path.relative_to(self.paths.root)),
            ):
                updated += 1
        self.database.set_metadata("wiki_render_version", WIKI_RENDER_VERSION)
        return {
            "documents_seen": seen,
            "documents_updated": updated,
            "documents_skipped": skipped,
            "render_version": WIKI_RENDER_VERSION,
        }

    def _download_asset(
        self,
        obj_token: str,
        *,
        file_token: str,
        asset_type: str,
        opener: Callable[[str], Any],
        block_id: str | None = None,
        filename: str | None = None,
    ) -> tuple[int, bool]:
        asset_id = self.database.ensure_wiki_asset(
            obj_token,
            file_token,
            asset_type,
            block_id=block_id,
            filename=filename,
        )
        existing = self.database.get_wiki_asset(asset_id) or {}
        local_path = resolve_archive_resource_path(
            self.paths.root,
            str(existing.get("local_path") or ""),
            legacy_anchor=("knowledge", "assets"),
        )
        if existing.get("status") == "downloaded" and local_path.is_file():
            return asset_id, False
        if self.database.wiki_asset_bytes() >= self.max_asset_bytes:
            self.database.update_wiki_asset(
                asset_id,
                status="skipped",
                error="知识库附件总容量已达到上限",
            )
            return asset_id, False
        response = None
        temp_path: Path | None = None
        try:
            response = opener(file_token)
            content_length = _optional_int(response.headers.get("Content-Length"))
            if content_length and content_length > self.max_single_asset_bytes:
                self.database.update_wiki_asset(
                    asset_id,
                    byte_size=content_length,
                    status="skipped",
                    error="单个知识库附件超过 100 MB 上限",
                )
                return asset_id, False
            response_filename = _response_filename(response.headers) or filename
            mime_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
            digest = hashlib.sha256()
            written = 0
            with tempfile.NamedTemporaryFile(
                prefix="wiki-asset-",
                dir=self.paths.knowledge_assets,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_single_asset_bytes:
                        raise AssetTooLargeError("单个知识库附件超过 100 MB 上限")
                    if self.database.wiki_asset_bytes() + written > self.max_asset_bytes:
                        raise AssetTooLargeError("知识库附件总容量已达到上限")
                    digest.update(chunk)
                    handle.write(chunk)
            sha256 = digest.hexdigest()
            suffix = _safe_suffix(response_filename, mime_type)
            destination = self.paths.knowledge_assets / sha256[:2] / f"{sha256}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                temp_path.unlink(missing_ok=True)
            else:
                temp_path.replace(destination)
                os.chmod(destination, 0o600)
            self.database.update_wiki_asset(
                asset_id,
                filename=response_filename,
                mime_type=mime_type or mimetypes.guess_type(response_filename or "")[0],
                byte_size=written,
                sha256=sha256,
                local_path=str(destination.relative_to(self.paths.root)),
                status="downloaded",
                error=None,
                downloaded_at=int(time.time() * 1000),
            )
            return asset_id, True
        except AssetTooLargeError as exc:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            self.database.update_wiki_asset(asset_id, status="skipped", error=str(exc))
            return asset_id, False
        except Exception as exc:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            self.database.update_wiki_asset(asset_id, status="error", error=str(exc))
            return asset_id, False
        finally:
            if response is not None:
                response.close()

    def _write_export(self, obj_token: str, title: str, rendered: str) -> Path:
        destination = self.paths.knowledge_exports / f"{_safe_token(obj_token)}.html"
        export_rendered = rendered
        for asset in self.database.list_wiki_assets(obj_token):
            local_path = resolve_archive_resource_path(
                self.paths.root,
                str(asset.get("local_path") or ""),
                legacy_anchor=("knowledge", "assets"),
            )
            if asset.get("status") != "downloaded" or not local_path.is_file():
                continue
            relative = Path(
                os.path.relpath(local_path, destination.parent.resolve())
            ).as_posix()
            export_rendered = export_rendered.replace(
                f'/api/wiki/assets/{asset["id"]}',
                html.escape(relative, quote=True),
            )
            export_rendered = export_rendered.replace(
                f'/api/wiki/preview/{asset["id"]}',
                html.escape(relative, quote=True),
            )
        page = (
            "<!doctype html><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title>"
            "<style>body{max-width:860px;margin:40px auto;padding:0 24px;"
            "font:16px/1.7 -apple-system,BlinkMacSystemFont,sans-serif;color:#1f2329}"
            "img{max-width:100%;height:auto}pre{white-space:pre-wrap;background:#f5f6f7;"
            "padding:16px;border-radius:8px}blockquote{border-left:4px solid #d0d3d6;"
            "margin-left:0;padding-left:16px;color:#646a73}</style>"
            f"<h1>{html.escape(title)}</h1>{export_rendered}"
        )
        destination.write_text(page, encoding="utf-8")
        os.chmod(destination, 0o600)
        return destination

    def _record_document_error(self, node: dict[str, Any], exc: Exception) -> None:
        self.database.mark_wiki_document_error(node, str(exc))


class AssetTooLargeError(RuntimeError):
    pass


def extract_assets(blocks: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        block_type = _optional_int(block.get("block_type"))
        block_id = str(block.get("block_id") or "")
        if block_type == 27:
            payload = block.get("image") or {}
            token = str(payload.get("token") or payload.get("file_token") or "")
            if token and (token, "image") not in seen:
                seen.add((token, "image"))
                assets.append(
                    {
                        "file_token": token,
                        "asset_type": "image",
                        "block_id": block_id,
                        "filename": str(payload.get("name") or "image"),
                    }
                )
        elif block_type == 23:
            payload = block.get("file") or {}
            token = str(payload.get("token") or payload.get("file_token") or "")
            if token and (token, "file") not in seen:
                seen.add((token, "file"))
                assets.append(
                    {
                        "file_token": token,
                        "asset_type": "file",
                        "block_id": block_id,
                        "filename": str(payload.get("name") or payload.get("file_name") or "file"),
                    }
                )
    return assets


def block_text(block: dict[str, Any]) -> str:
    block_type = _optional_int(block.get("block_type"))
    name = BLOCK_NAMES.get(block_type or 0)
    payload = block.get(name) if name else None
    if not isinstance(payload, dict):
        return ""
    elements = payload.get("elements")
    if isinstance(elements, list):
        return "".join(_element_text(element) for element in elements)
    if name == "page":
        return str(payload.get("elements") or payload.get("title") or "")
    return ""


def render_blocks(
    blocks: Iterable[dict[str, Any]],
    asset_ids: dict[tuple[str, str], int],
    asset_states: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> str:
    asset_states = asset_states or {}
    parts: list[str] = []
    ordered_number = 0
    for block in blocks:
        block_type = _optional_int(block.get("block_type")) or 0
        text_value = _block_rich_text(block)
        if block_type == 1:
            continue
        if block_type == 22:
            parts.append("<hr>")
        elif block_type == 27:
            payload = block.get("image") or {}
            token = str(payload.get("token") or payload.get("file_token") or "")
            asset_id = asset_ids.get((token, "image"))
            state = asset_states.get((token, "image"))
            downloaded = bool(asset_id and (state is None or state.get("status") == "downloaded"))
            caption = html.escape(str(payload.get("name") or ""))
            if downloaded:
                parts.append(
                    f'<figure><a class="kb-image-link" href="/api/wiki/assets/{asset_id}" '
                    f'target="_blank" rel="noopener"><img loading="lazy" decoding="async" '
                    f'src="/api/wiki/assets/{asset_id}" alt="{caption}"></a>'
                    f'{f"<figcaption>{caption}</figcaption>" if caption else ""}</figure>'
                )
            else:
                detail = html.escape(str((state or {}).get("error") or (state or {}).get("status") or "未下载"))
                parts.append(f'<div class="kb-resource-placeholder">图片未归档：{detail}</div>')
        elif block_type == 23:
            payload = block.get("file") or {}
            token = str(payload.get("token") or payload.get("file_token") or "")
            asset_id = asset_ids.get((token, "file"))
            state = asset_states.get((token, "file"))
            label = html.escape(str(payload.get("name") or payload.get("file_name") or "附件"))
            downloaded = bool(asset_id and (state is None or state.get("status") == "downloaded"))
            if downloaded:
                parts.append(
                    f'<p><a class="kb-file" href="/api/wiki/preview/{asset_id}" '
                    f'target="_blank" rel="noopener">{label}</a></p>'
                )
            else:
                detail = html.escape(str((state or {}).get("error") or (state or {}).get("status") or "未下载"))
                parts.append(f'<div class="kb-resource-placeholder">{label}：{detail}</div>')
        elif 3 <= block_type <= 11:
            level = min(block_type - 2, 6)
            if text_value:
                parts.append(f"<h{level}>{text_value}</h{level}>")
        elif block_type == 12 and text_value:
            ordered_number = 0
            parts.append(f"<ul><li>{text_value}</li></ul>")
        elif block_type == 13 and text_value:
            ordered_number += 1
            parts.append(f'<ol start="{ordered_number}"><li>{text_value}</li></ol>')
        elif block_type == 14 and text_value:
            ordered_number = 0
            parts.append(f"<pre><code>{text_value}</code></pre>")
        elif block_type in {15, 34} and text_value:
            ordered_number = 0
            parts.append(f"<blockquote>{text_value}</blockquote>")
        elif block_type == 17 and text_value:
            ordered_number = 0
            done = bool((block.get("todo") or {}).get("done"))
            parts.append(f'<p class="kb-todo">{"☑" if done else "☐"} {text_value}</p>')
        elif block_type == 19 and text_value:
            ordered_number = 0
            parts.append(f'<aside class="kb-callout">{text_value}</aside>')
        elif text_value:
            ordered_number = 0
            parts.append(f"<p>{text_value}</p>")
    return "\n".join(parts)


def render_file_asset(title: str, asset: dict[str, Any] | None) -> str:
    label = html.escape(title or "附件")
    if asset and asset.get("status") == "downloaded":
        return (
            f'<p><a class="kb-file" href="/api/wiki/preview/{int(asset["id"])}" '
            f'target="_blank" rel="noopener">{label}</a></p>'
        )
    detail = html.escape(str((asset or {}).get("error") or (asset or {}).get("status") or "未下载"))
    return f'<div class="kb-resource-placeholder">{label}：{detail}</div>'


def _block_rich_text(block: dict[str, Any]) -> str:
    block_type = _optional_int(block.get("block_type"))
    name = BLOCK_NAMES.get(block_type or 0)
    payload = block.get(name) if name else None
    if not isinstance(payload, dict):
        return ""
    elements = payload.get("elements")
    if isinstance(elements, list):
        return "".join(_render_element(element) for element in elements)
    if name == "page":
        return html.escape(str(payload.get("elements") or payload.get("title") or ""))
    return ""


def _render_element(element: Any) -> str:
    if not isinstance(element, dict):
        return ""
    text_run = element.get("text_run")
    if isinstance(text_run, dict):
        content = html.escape(str(text_run.get("content") or "")).replace("\n", "<br>")
        style = text_run.get("text_element_style") or {}
        if not isinstance(style, dict):
            style = {}
        if style.get("inline_code"):
            content = f"<code>{content}</code>"
        if style.get("bold"):
            content = f"<strong>{content}</strong>"
        if style.get("italic"):
            content = f"<em>{content}</em>"
        if style.get("strikethrough"):
            content = f"<s>{content}</s>"
        if style.get("underline"):
            content = f"<u>{content}</u>"
        link = style.get("link") or {}
        url = link.get("url") if isinstance(link, dict) else None
        safe_url = _safe_external_url(url)
        if safe_url:
            content = (
                f'<a class="kb-external-link" href="{html.escape(safe_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{content or html.escape(safe_url)}</a>'
            )
        return content
    mention_doc = element.get("mention_doc")
    if isinstance(mention_doc, dict):
        label = html.escape(str(mention_doc.get("title") or mention_doc.get("url") or "知识库文档"))
        safe_url = _safe_external_url(mention_doc.get("url"))
        if safe_url:
            return (
                f'<a class="kb-external-link" href="{html.escape(safe_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{label}</a>'
            )
        return label
    return html.escape(_element_text(element)).replace("\n", "<br>")


def _safe_external_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        url = urllib.parse.unquote(url)
        parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _element_text(element: Any) -> str:
    if not isinstance(element, dict):
        return ""
    text_run = element.get("text_run")
    if isinstance(text_run, dict):
        return str(text_run.get("content") or "")
    mention_doc = element.get("mention_doc")
    if isinstance(mention_doc, dict):
        return str(mention_doc.get("title") or mention_doc.get("url") or "")
    mention_user = element.get("mention_user")
    if isinstance(mention_user, dict):
        return str(mention_user.get("name") or mention_user.get("user_id") or "")
    reminder = element.get("reminder")
    if isinstance(reminder, dict):
        return str(reminder.get("text") or "")
    equation = element.get("equation")
    if isinstance(equation, dict):
        return str(equation.get("content") or "")
    return ""


def _response_filename(headers: Any) -> str | None:
    disposition = str(headers.get("Content-Disposition") or "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if match:
        return Path(urllib.parse.unquote(match.group(1))).name
    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
    if match:
        return Path(match.group(1).strip()).name
    return None


def _safe_suffix(filename: str | None, mime_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not suffix and mime_type:
        suffix = mimetypes.guess_extension(mime_type) or ""
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return ""
    return suffix


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:180] or "document"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_current(existing: dict[str, Any] | None, source_edit_time: int | None) -> bool:
    if not existing or existing.get("status") not in {"synced", "metadata_only"}:
        return False
    previous = _optional_int(existing.get("source_edit_time"))
    if source_edit_time is None or previous is None:
        return False
    return previous >= source_edit_time
