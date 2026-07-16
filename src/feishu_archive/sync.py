from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ArchivePaths, MAX_SINGLE_ATTACHMENT_BYTES
from .database import ArchiveDatabase
from .feishu import FeishuAPIError, FeishuClient
from .parser import ResourceRef, normalize_message


@dataclass
class SyncCounts:
    messages_seen: int = 0
    messages_written: int = 0
    attachments_downloaded: int = 0
    attachments_skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "messages_seen": self.messages_seen,
            "messages_written": self.messages_written,
            "attachments_downloaded": self.attachments_downloaded,
            "attachments_skipped": self.attachments_skipped,
        }


class ArchiveSyncer:
    def __init__(
        self,
        database: ArchiveDatabase,
        client: FeishuClient,
        paths: ArchivePaths,
        *,
        max_attachment_bytes: int,
    ) -> None:
        self.database = database
        self.client = client
        self.paths = paths
        self.max_attachment_bytes = max(0, max_attachment_bytes)

    def discover(self) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        for page in self.client.iter_chat_pages():
            for item in page.get("items") or []:
                self.database.upsert_conversation(item)
                chats.append(item)
        return chats

    def sync(self, chat_ids: list[str], *, days: int, skip_attachments: bool = False) -> SyncCounts:
        if not chat_ids:
            raise ValueError("至少需要一个 chat_id")
        if days < 1:
            raise ValueError("days 必须大于 0")
        unique_chat_ids = list(dict.fromkeys(chat_ids))
        run_id = self.database.start_sync_run(unique_chat_ids, days)
        counts = SyncCounts()
        now_s = int(time.time())
        start_s = now_s - days * 86400
        errors: list[str] = []
        try:
            for chat_id in unique_chat_ids:
                try:
                    self._sync_chat(chat_id, start_s, now_s, counts)
                except Exception as exc:
                    message = f"{chat_id}: {exc}"
                    errors.append(message)
                    self.database.set_sync_state(
                        "chat",
                        chat_id,
                        window_start=start_s,
                        window_end=now_s,
                        page_token=None,
                        status="error",
                        error=str(exc),
                    )
            attachment_failures = 0
            if not skip_attachments:
                attachment_failures = self._download_pending(unique_chat_ids, counts)
            if errors and len(errors) == len(unique_chat_ids):
                status = "error"
            elif errors or attachment_failures:
                status = "partial"
            else:
                status = "success"
            if attachment_failures:
                errors.append(f"{attachment_failures} 个附件下载失败，可在下次同步时自动重试")
            self.database.finish_sync_run(
                run_id,
                status=status,
                error="\n".join(errors) or None,
                **counts.as_dict(),
            )
        except Exception as exc:
            self.database.finish_sync_run(
                run_id,
                status="error",
                error=str(exc),
                **counts.as_dict(),
            )
            raise
        return counts

    def _sync_chat(self, chat_id: str, start_s: int, end_s: int, counts: SyncCounts) -> None:
        self.database.ensure_conversation(chat_id)
        member_names = self._sync_members(chat_id)
        self.database.set_sync_state(
            "chat",
            chat_id,
            window_start=start_s,
            window_end=end_s,
            page_token=None,
            status="running",
        )
        thread_ids: set[str] = set()
        last_message_at: int | None = None
        for page in self.client.iter_message_pages(
            "chat", chat_id, start_time=start_s, end_time=end_s
        ):
            for item in page.get("items") or []:
                normalized = normalize_message(item, chat_id)
                if not normalized.get("sender_name") and normalized.get("sender_id"):
                    normalized["sender_name"] = member_names.get(str(normalized["sender_id"]))
                counts.messages_seen += 1
                counts.messages_written += int(self.database.upsert_message(normalized))
                self._record_resources(normalized["message_id"], normalized["resources"])
                if normalized.get("thread_id"):
                    thread_ids.add(str(normalized["thread_id"]))
                created_at = normalized.get("created_at")
                if created_at is not None:
                    last_message_at = max(last_message_at or created_at, created_at)
            self.database.set_sync_state(
                "chat",
                chat_id,
                window_start=start_s,
                window_end=end_s,
                page_token=page.get("page_token") if page.get("has_more") else None,
                status="running",
                last_message_at=last_message_at,
            )

        start_ms, end_ms = start_s * 1000, end_s * 1000 + 999
        for thread_id in sorted(thread_ids):
            self._sync_thread(thread_id, chat_id, start_ms, end_ms, counts, member_names)

        self.database.set_sync_state(
            "chat",
            chat_id,
            window_start=start_s,
            window_end=end_s,
            page_token=None,
            status="success",
            last_message_at=last_message_at,
        )

    def _sync_thread(
        self,
        thread_id: str,
        chat_id: str,
        start_ms: int,
        end_ms: int,
        counts: SyncCounts,
        member_names: dict[str, str],
    ) -> None:
        self.database.set_sync_state(
            "thread",
            thread_id,
            window_start=start_ms // 1000,
            window_end=end_ms // 1000,
            page_token=None,
            status="running",
        )
        for page in self.client.iter_message_pages("thread", thread_id):
            for item in page.get("items") or []:
                normalized = normalize_message(item, chat_id)
                if not normalized.get("sender_name") and normalized.get("sender_id"):
                    normalized["sender_name"] = member_names.get(str(normalized["sender_id"]))
                created_at = normalized.get("created_at")
                if created_at is not None and not (start_ms <= created_at <= end_ms):
                    continue
                counts.messages_seen += 1
                counts.messages_written += int(self.database.upsert_message(normalized))
                self._record_resources(normalized["message_id"], normalized["resources"])
        self.database.set_sync_state(
            "thread",
            thread_id,
            window_start=start_ms // 1000,
            window_end=end_ms // 1000,
            page_token=None,
            status="success",
        )

    def _sync_members(self, chat_id: str) -> dict[str, str]:
        try:
            for page in self.client.iter_member_pages(chat_id):
                for item in page.get("items") or []:
                    self.database.upsert_member(chat_id, item)
            self.database.set_sync_state(
                "members",
                chat_id,
                window_start=None,
                window_end=None,
                page_token=None,
                status="success",
            )
        except FeishuAPIError as exc:
            self.database.set_sync_state(
                "members",
                chat_id,
                window_start=None,
                window_end=None,
                page_token=None,
                status="error",
                error=str(exc),
            )
        return self.database.member_names(chat_id)

    def _record_resources(self, message_id: str, resources: list[ResourceRef]) -> None:
        for resource in resources:
            self.database.ensure_attachment(
                message_id,
                resource.file_key,
                resource.resource_type,
                resource.filename,
            )

    def _download_pending(self, chat_ids: list[str], counts: SyncCounts) -> int:
        allowed_chats = set(chat_ids)
        used_bytes = self.database.attachment_bytes()
        failures = 0
        for attachment in self.database.list_pending_attachments():
            if attachment["chat_id"] not in allowed_chats:
                continue
            if used_bytes >= self.max_attachment_bytes:
                self.database.update_attachment(
                    attachment["id"], status="skipped_capacity", error="已达到附件总容量上限"
                )
                counts.attachments_skipped += 1
                continue
            try:
                byte_size = self._download_one(attachment, self.max_attachment_bytes - used_bytes)
                used_bytes += byte_size
                counts.attachments_downloaded += 1
            except CapacityError as exc:
                self.database.update_attachment(
                    attachment["id"], status=exc.status, error=str(exc)
                )
                counts.attachments_skipped += 1
            except FeishuAPIError as exc:
                if "size exceeds limit" in str(exc).lower():
                    self.database.update_attachment(
                        attachment["id"], status="skipped_too_large", error=str(exc)
                    )
                    counts.attachments_skipped += 1
                else:
                    self.database.update_attachment(
                        attachment["id"], status="error", error=str(exc)
                    )
                    failures += 1
            except OSError as exc:
                self.database.update_attachment(
                    attachment["id"], status="error", error=f"附件下载失败：{exc}"
                )
                failures += 1
        return failures

    def _download_one(self, attachment: dict[str, Any], remaining_bytes: int) -> int:
        with self.client.open_resource(
            attachment["message_id"], attachment["file_key"], attachment["resource_type"]
        ) as response:
            content_length_value = response.headers.get("Content-Length")
            content_length = int(content_length_value) if content_length_value else None
            if content_length is not None and content_length > MAX_SINGLE_ATTACHMENT_BYTES:
                raise CapacityError("资源超过飞书接口 100 MB 上限", "skipped_too_large")
            if content_length is not None and content_length > remaining_bytes:
                raise CapacityError("资源会超过附件总容量上限", "skipped_capacity")

            filename = _safe_filename(attachment.get("filename") or f"resource-{attachment['id']}")
            relative_dir = Path("attachments") / _safe_component(attachment["chat_id"])
            target_dir = self.paths.root / relative_dir
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = target_dir / f"{attachment['id']}-{filename}"
            temporary = target.with_suffix(target.suffix + ".part")
            digest = hashlib.sha256()
            size = 0
            try:
                with temporary.open("wb") as output:
                    os.chmod(temporary, 0o600)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_SINGLE_ATTACHMENT_BYTES:
                            raise CapacityError("资源实际大小超过 100 MB", "skipped_too_large")
                        if size > remaining_bytes:
                            raise CapacityError("资源实际大小会超过附件总容量上限", "skipped_capacity")
                        digest.update(chunk)
                        output.write(chunk)
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        self.database.update_attachment(
            attachment["id"],
            mime_type=response.headers.get("Content-Type"),
            byte_size=size,
            sha256=digest.hexdigest(),
            local_path=str(target.relative_to(self.paths.root)),
            status="downloaded",
            error=None,
            downloaded_at=int(time.time() * 1000),
        )
        return size


class CapacityError(RuntimeError):
    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:100] or "unknown"


def _safe_filename(value: str) -> str:
    name = Path(value).name
    cleaned = re.sub(r"[\x00-\x1f/:\\]+", "_", name).strip(" .")
    return cleaned[:180] or "resource"
