from __future__ import annotations

import html
import ipaddress
import json
import mimetypes
import socket
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from .config import ArchivePaths
from .database import ArchiveDatabase


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def is_loopback_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    return all(ipaddress.ip_address(address).is_loopback for address in addresses)


class ArchiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        database: ArchiveDatabase,
        paths: ArchivePaths,
        *,
        sync_start: Callable[[], bool] | None = None,
        sync_schedule: dict[str, Any] | None = None,
        wiki_sync_start: Callable[[], bool] | None = None,
        wiki_sync_schedule: dict[str, Any] | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.sync_start = sync_start
        self.sync_schedule = sync_schedule or {"enabled": False}
        self.wiki_sync_start = wiki_sync_start
        self.wiki_sync_schedule = wiki_sync_schedule or {"enabled": False}
        super().__init__(server_address, ArchiveRequestHandler)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                self._json(self.server.database.status())
            elif parsed.path == "/api/sync/status":
                self._json(
                    {
                        "job": self.server.database.latest_sync_job(),
                        "schedule": self.server.sync_schedule,
                    }
                )
            elif parsed.path == "/api/conversations":
                self._json({"items": self.server.database.list_conversations()})
            elif parsed.path == "/api/wiki/status":
                self._json(
                    {
                        **self.server.database.wiki_status(),
                        "schedule": self.server.wiki_sync_schedule,
                    }
                )
            elif parsed.path == "/api/wiki/spaces":
                self._json({"items": self.server.database.list_wiki_spaces()})
            elif parsed.path == "/api/wiki/nodes":
                space_id = _first(query, "space_id")
                if not space_id:
                    raise ValueError("缺少 space_id")
                self._json({"items": self.server.database.list_wiki_nodes(space_id)})
            elif parsed.path == "/api/wiki/document":
                node_token = _first(query, "node_token")
                if not node_token:
                    raise ValueError("缺少 node_token")
                document = self.server.database.wiki_document_for_node(node_token)
                if not document:
                    self.send_error(HTTPStatus.NOT_FOUND, "Document not found")
                else:
                    document["assets"] = self.server.database.list_wiki_assets(
                        str(document.get("obj_token") or "")
                    )
                    self._json(document)
            elif parsed.path == "/api/wiki/search":
                query_value = _first(query, "q") or ""
                self._json(
                    {
                        "items": self.server.database.search_wiki_documents(
                            query_value,
                            space_id=_first(query, "space_id"),
                            limit=int(_first(query, "limit") or 100),
                        )
                    }
                )
            elif parsed.path.startswith("/api/wiki/preview/"):
                self._wiki_preview(parsed.path)
            elif parsed.path.startswith("/api/wiki/assets/"):
                self._wiki_resource(parsed.path, query)
            elif parsed.path == "/api/senders":
                chat_id = _first(query, "chat_id")
                self._json({"items": self.server.database.list_senders(chat_id)})
            elif parsed.path == "/api/messages":
                self._messages(query)
            elif parsed.path == "/api/export":
                self._export(query)
            elif parsed.path.startswith("/api/images/"):
                self._resource(parsed.path, "/api/images/", "image", download=False)
            elif parsed.path.startswith("/api/attachments/"):
                self._resource(parsed.path, "/api/attachments/", "file", download=True)
            elif parsed.path == "/":
                self._static("index.html")
            elif parsed.path.startswith("/static/"):
                self._static(parsed.path.removeprefix("/static/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception:
            self._json({"error": "本地阅读器处理请求失败"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            actions = {
                "/api/sync": ("sync", self.server.sync_start),
                "/api/wiki/sync": ("wiki-sync", self.server.wiki_sync_start),
            }
            action = actions.get(parsed.path)
            if action is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            action_name, starter = action
            if self.headers.get("X-Feishu-Archive-Action") != action_name:
                self._json({"error": "缺少本机同步确认标头"}, status=HTTPStatus.FORBIDDEN)
                return
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urllib.parse.urlparse(origin).hostname
                if not origin_host or not is_loopback_host(origin_host):
                    self._json({"error": "拒绝非本机来源"}, status=HTTPStatus.FORBIDDEN)
                    return
            if starter is None:
                self._json({"error": "当前阅读器未启用同步控制"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not starter():
                self._json({"error": "已有同步任务正在运行"}, status=HTTPStatus.CONFLICT)
                return
            self._json({"status": "accepted"}, status=HTTPStatus.ACCEPTED)
        except Exception:
            self._json({"error": "启动同步失败"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _messages(self, query: dict[str, list[str]]) -> None:
        date_from = _date_to_ms(_first(query, "date_from"))
        date_to_value = _first(query, "date_to")
        date_to = _date_to_ms(date_to_value, end_of_day=True) if date_to_value else None
        items = self.server.database.query_messages(
            chat_id=_first(query, "chat_id"),
            query=_first(query, "q"),
            sender=_first(query, "sender"),
            message_type=_first(query, "type"),
            date_from_ms=date_from,
            date_to_ms=date_to,
            limit=int(_first(query, "limit") or 200),
            offset=int(_first(query, "offset") or 0),
        )
        resources = self.server.database.resources_for_messages(
            [str(item["message_id"]) for item in items]
        )
        for item in items:
            item["resources"] = resources.get(str(item["message_id"]), [])
        self._json({"items": items})

    def _export(self, query: dict[str, list[str]]) -> None:
        chat_id = _first(query, "chat_id")
        export_format = (_first(query, "format") or "json").lower()
        if not chat_id:
            raise ValueError("导出需要 chat_id")
        conversations = {
            item["chat_id"]: item for item in self.server.database.list_conversations()
        }
        conversation = conversations.get(chat_id)
        if not conversation:
            raise ValueError("会话不存在")
        messages: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.server.database.query_messages(
                chat_id=chat_id,
                limit=500,
                offset=offset,
                newest_first=False,
            )
            messages.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        filename_base = _download_name(conversation.get("name") or chat_id)
        if export_format == "json":
            resources = self.server.database.resources_for_messages(
                [str(message["message_id"]) for message in messages]
            )
            for message in messages:
                message["resources"] = resources.get(str(message["message_id"]), [])
            body = json.dumps(
                {"conversation": conversation, "messages": messages},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self._bytes(
                body,
                "application/json; charset=utf-8",
                filename=f"{filename_base}.json",
            )
            return
        if export_format == "html":
            body = _conversation_html(conversation, messages).encode("utf-8")
            self._bytes(body, "text/html; charset=utf-8", filename=f"{filename_base}.html")
            return
        raise ValueError("format 必须是 json 或 html")

    def _resource(
        self,
        path: str,
        prefix: str,
        expected_type: str,
        *,
        download: bool,
    ) -> None:
        raw_id = path.removeprefix(prefix)
        if not raw_id.isdigit():
            raise ValueError("资源 ID 无效")
        attachment = self.server.database.get_attachment(int(raw_id))
        if (
            not attachment
            or attachment.get("resource_type") != expected_type
            or attachment.get("status") != "downloaded"
            or not attachment.get("local_path")
        ):
            self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")
            return
        root = self.server.paths.root.resolve()
        target = (root / str(attachment["local_path"])).resolve()
        if root not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")
            return
        content_type = attachment.get("mime_type") or mimetypes.guess_type(target.name)[0]
        self._bytes(
            target.read_bytes(),
            content_type or "application/octet-stream",
            filename=(attachment.get("filename") or target.name) if download else None,
        )

    def _wiki_asset_target(self, raw_id: str) -> tuple[dict[str, Any], Path] | None:
        if not raw_id.isdigit():
            raise ValueError("资源 ID 无效")
        asset = self.server.database.get_wiki_asset(int(raw_id))
        if (
            not asset
            or asset.get("status") != "downloaded"
            or not asset.get("local_path")
        ):
            self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")
            return None
        root = self.server.paths.root.resolve()
        target = Path(str(asset["local_path"])).resolve()
        if root not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")
            return None
        return asset, target

    def _wiki_resource(self, path: str, query: dict[str, list[str]]) -> None:
        raw_id = path.removeprefix("/api/wiki/assets/")
        resolved = self._wiki_asset_target(raw_id)
        if not resolved:
            return
        asset, target = resolved
        content_type = asset.get("mime_type") or mimetypes.guess_type(target.name)[0]
        download = _first(query, "download") == "1"
        filename = asset.get("filename") or target.name
        self._bytes(
            target.read_bytes(),
            content_type or "application/octet-stream",
            filename=filename if download or not _inline_wiki_mime(content_type) else None,
        )

    def _wiki_preview(self, path: str) -> None:
        raw_id = path.removeprefix("/api/wiki/preview/")
        resolved = self._wiki_asset_target(raw_id)
        if not resolved:
            return
        asset, target = resolved
        filename = str(asset.get("filename") or target.name)
        content_type = str(asset.get("mime_type") or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        asset_url = f"/api/wiki/assets/{raw_id}"
        download_url = f"{asset_url}?download=1"
        safe_name = html.escape(filename)
        safe_type = html.escape(content_type)
        size_text = html.escape(_format_bytes(int(asset.get("byte_size") or target.stat().st_size)))
        kind = _wiki_preview_kind(content_type)
        if kind == "frame":
            preview = (
                f'<iframe class="asset-preview-frame" src="{asset_url}" '
                f'title="{safe_name}"></iframe>'
            )
        elif kind == "image":
            preview = f'<img class="asset-preview-media" src="{asset_url}" alt="{safe_name}">'
        elif kind == "audio":
            preview = f'<audio class="asset-preview-media" controls src="{asset_url}"></audio>'
        elif kind == "video":
            preview = f'<video class="asset-preview-media" controls src="{asset_url}"></video>'
        else:
            preview = (
                '<div class="asset-preview-empty">当前浏览器不能直接预览此格式。'
                '文件已经保存在本机，可使用右上角按钮下载并用系统应用打开。</div>'
            )
        body = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{safe_name} - Feishu Archive</title>'
            '<link rel="stylesheet" href="/static/styles.css"></head>'
            '<body><main class="asset-preview"><section class="asset-preview-card">'
            '<header class="asset-preview-head"><div>'
            f'<h1>{safe_name}</h1><p>{safe_type} · {size_text} · 本机离线文件</p></div>'
            f'<a class="button" href="{download_url}">下载文件</a></header>{preview}'
            '</section></main></body></html>'
        ).encode("utf-8")
        self._bytes(body, "text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        resource = files("feishu_archive").joinpath("static", name)
        if not resource.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = resource.read_bytes()
        self._bytes(body, STATIC_TYPES.get(Path(name).suffix, "application/octet-stream"))

    def _json(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._bytes(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        filename: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if filename:
            encoded = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[reader] {self.client_address[0]} {fmt % args}")


def serve(
    database: ArchiveDatabase,
    paths: ArchivePaths,
    host: str,
    port: int,
    *,
    sync_start: Callable[[], bool] | None = None,
    sync_schedule: dict[str, Any] | None = None,
    wiki_sync_start: Callable[[], bool] | None = None,
    wiki_sync_schedule: dict[str, Any] | None = None,
) -> None:
    if not is_loopback_host(host):
        raise ValueError("安全限制：离线阅读器只能监听回环地址 127.0.0.1 或 localhost")
    server = ArchiveHTTPServer(
        (host, port),
        database,
        paths,
        sync_start=sync_start,
        sync_schedule=sync_schedule,
        wiki_sync_start=wiki_sync_start,
        wiki_sync_schedule=wiki_sync_schedule,
    )
    print(f"Feishu Archive 阅读器：http://{host}:{port}")
    print("按 Ctrl+C 停止。阅读器不会监听局域网地址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _date_to_ms(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").astimezone()
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
    milliseconds = int(parsed.timestamp() * 1000)
    return milliseconds + (86400000 if end_of_day else 0)


def _download_name(value: str) -> str:
    allowed = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in value)
    return allowed.strip(" .")[:100] or "feishu-conversation"


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _inline_wiki_mime(content_type: str | None) -> bool:
    value = str(content_type or "").lower().split(";", 1)[0]
    return (
        value.startswith("image/")
        or value.startswith("audio/")
        or value.startswith("video/")
        or value == "application/pdf"
        or value in {"text/plain", "text/csv", "application/json"}
    )


def _wiki_preview_kind(content_type: str) -> str | None:
    value = content_type.lower().split(";", 1)[0]
    if value == "application/pdf" or value in {"text/plain", "text/csv", "application/json"}:
        return "frame"
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    return None


def _conversation_html(conversation: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    rows = []
    for message in messages:
        timestamp = message.get("created_at")
        if timestamp:
            time_text = datetime.fromtimestamp(timestamp / 1000).astimezone().isoformat(timespec="seconds")
        else:
            time_text = ""
        flags = []
        if message.get("deleted"):
            flags.append("已删除")
        if message.get("recalled"):
            flags.append("已撤回")
        flag_text = f" · {' / '.join(flags)}" if flags else ""
        rows.append(
            "<article><header>"
            f"<strong>{html.escape(message.get('sender_name') or '未知发送者')}</strong>"
            f"<time>{html.escape(time_text)}</time>"
            "</header>"
            f"<pre>{html.escape(message.get('body_text') or '')}</pre>"
            f"<small>{html.escape(message.get('message_type') or '')}{html.escape(flag_text)}</small>"
            "</article>"
        )
    title = html.escape(conversation.get("name") or conversation["chat_id"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} - Feishu Archive</title>
<style>
body{{max-width:860px;margin:40px auto;padding:0 20px;background:#f4f6f8;color:#17212b;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
article{{background:#fff;border:1px solid #dfe4ea;border-radius:12px;padding:14px 16px;margin:12px 0}}
header{{display:flex;justify-content:space-between;gap:16px}} time,small{{color:#65717d}} pre{{white-space:pre-wrap;font:inherit;margin:8px 0}}
</style></head><body><h1>{title}</h1><p>{len(messages)} 条消息 · 导出时间 {html.escape(datetime.now().astimezone().isoformat(timespec='seconds'))}</p>{''.join(rows)}</body></html>"""
