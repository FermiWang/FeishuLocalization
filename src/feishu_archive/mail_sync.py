from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import (
    DEFAULT_MAIL_INITIAL_DAYS,
    DEFAULT_MAIL_MAX_PAGES,
    DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_BYTES,
    MAIL_SCOPES,
    ArchivePaths,
)
from .feishu import FeishuAPIError
from .mail_database import MailDatabase
from .mail_provider import MailMessage, MailProvider


MAIL_SEARCH_TOKEN_EXPIRED = 1231020
MAIL_SEARCH_PAGE_LIMIT = 1231022
MAIL_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAIL_HARD_STOP_FREE_BYTES = 75 * 1024**3
MAIL_ATTACHMENT_STOP_FREE_BYTES = 100 * 1024**3
MAIL_BATCH_RETRY_DELAYS = (0.25, 1.0)
MAIL_LIST_MIN_INTERVAL_SECONDS = 0.11
MAIL_BATCH_MIN_INTERVAL_SECONDS = 0.11
MAIL_ATTACHMENT_URL_MIN_INTERVAL_SECONDS = 1.05

SYSTEM_MAIL_FOLDERS = (
    ("INBOX", "收件箱", "inbox"),
    ("SENT", "已发送", "sent"),
    ("DRAFT", "草稿", "draft"),
    ("SCHEDULED", "定时发送", "scheduled"),
    ("TRASH", "垃圾箱", "trash"),
    ("SPAM", "垃圾邮件", "spam"),
    ("ARCHIVED", "已归档", "archive"),
)


class MailSyncPartialError(RuntimeError):
    pass


class MailCapacityError(RuntimeError):
    pass


class MailAuthorizationError(RuntimeError):
    pass


@dataclass
class MailSyncCounts:
    folders_seen: int = 0
    windows_scanned: int = 0
    pages_scanned: int = 0
    message_ids_seen: int = 0
    messages_seen: int = 0
    messages_written: int = 0
    raw_messages_saved: int = 0
    attachments_seen: int = 0
    attachments_downloaded: int = 0
    attachments_skipped: int = 0
    bytes_downloaded: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "folders_seen": self.folders_seen,
            "windows_scanned": self.windows_scanned,
            "pages_scanned": self.pages_scanned,
            "message_ids_seen": self.message_ids_seen,
            "messages_seen": self.messages_seen,
            "messages_written": self.messages_written,
            "raw_messages_saved": self.raw_messages_saved,
            "attachments_seen": self.attachments_seen,
            "attachments_downloaded": self.attachments_downloaded,
            "attachments_skipped": self.attachments_skipped,
            "bytes_downloaded": self.bytes_downloaded,
        }


@dataclass(frozen=True)
class MailFolderTarget:
    provider_folder_id: str
    search_value: str | None
    list_label_id: str | None = None


class MailSyncer:
    def __init__(
        self,
        database: MailDatabase,
        provider: MailProvider,
        paths: ArchivePaths,
        *,
        max_mail_bytes: int = DEFAULT_MAX_MAIL_BYTES,
        max_attachment_bytes: int = DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    ) -> None:
        if max_mail_bytes < 0 or max_attachment_bytes < 0:
            raise ValueError("邮箱容量上限不能小于 0")
        self.database = database
        self.provider = provider
        self.paths = paths
        self.max_mail_bytes = max_mail_bytes
        self.max_attachment_bytes = max_attachment_bytes
        self.paths.ensure()
        self._used_bytes = int(self.database.status().get("blob_bytes") or 0)
        self._last_list_request_at = 0.0
        self._last_batch_request_at = 0.0
        self._last_attachment_url_request_at = 0.0

    def sync(
        self,
        *,
        folders: Iterable[str] | None = None,
        days: int | None = DEFAULT_MAIL_INITIAL_DAYS,
        skip_attachments: bool = False,
        trigger: str = "manual",
        max_pages: int = DEFAULT_MAIL_MAX_PAGES,
    ) -> MailSyncCounts:
        if days is not None and days < 1:
            raise ValueError("days 必须大于 0")
        if max_pages < 1:
            raise ValueError("max_pages 必须大于 0")
        self._verify_granted_scopes()
        self._check_write_capacity()

        profile = dict(self.provider.profile("me"))
        mailbox_address = str(
            profile.get("primary_email_address") or profile.get("mailbox_id") or ""
        ).strip()
        if not mailbox_address:
            raise ValueError("飞书邮箱 profile 未返回主邮箱地址")
        profile.update(
            {
                "provider": "feishu",
                "provider_mailbox_id": mailbox_address,
                "primary_email_address": mailbox_address,
            }
        )
        folder_items = _deduplicate_folder_items(
            dict(item) for item in self.provider.list_folders(mailbox_address)
        )
        known_folder_ids = {
            str(item.get("id") or "").strip().casefold() for item in folder_items
        }
        for folder_id, name, _search_name in SYSTEM_MAIL_FOLDERS:
            if folder_id.casefold() not in known_folder_ids:
                folder_items.append(
                    {
                        "id": folder_id,
                        "name": name,
                        "folder_type": 1,
                        "local_system_folder": True,
                    }
                )
                known_folder_ids.add(folder_id.casefold())
        selected_folders = _selected_folder_targets(
            folder_items,
            folders,
            require_search_paths=days is not None,
        )

        mailbox_row_id = self.database.upsert_mailbox(profile)
        self.database.replace_folders(mailbox_row_id, folder_items)
        counts = MailSyncCounts(folders_seen=len(folder_items))

        now = datetime.now().astimezone().replace(microsecond=0)
        window_end_ms = int(now.timestamp() * 1000)
        start = now - timedelta(days=days) if days is not None else None
        run_id = self.database.start_sync_run(
            mailbox_row_id,
            trigger,
            window_start=int(start.timestamp() * 1000) if start is not None else None,
            window_end=window_end_ms,
        )
        errors: list[str] = []
        seen_ids: set[str] = set()
        page_budget = [max_pages]
        try:
            if start is None:
                for folder in selected_folders:
                    scope = f"folder:{folder.provider_folder_id}"
                    pages_before = counts.pages_scanned
                    ids_before = counts.message_ids_seen
                    errors_before = len(errors)
                    state_extra = {
                        "mode": "full",
                        "run_id": run_id,
                        "label_id": folder.list_label_id,
                    }
                    self.database.set_sync_state(
                        mailbox_row_id,
                        scope,
                        window_end=window_end_ms,
                        status="running",
                        extra=state_extra,
                    )
                    try:
                        self._scan_folder_pages(
                            mailbox_row_id,
                            mailbox_address,
                            folder,
                            seen_ids,
                            counts,
                            errors,
                            page_budget,
                            skip_attachments=skip_attachments,
                        )
                    except Exception as exc:
                        self.database.set_sync_state(
                            mailbox_row_id,
                            scope,
                            window_end=window_end_ms,
                            status="error",
                            error=_safe_error(exc),
                            extra={
                                **state_extra,
                                "pages": counts.pages_scanned - pages_before,
                                "message_ids": counts.message_ids_seen - ids_before,
                            },
                        )
                        raise
                    counts.windows_scanned += 1
                    folder_status = (
                        "partial" if len(errors) > errors_before else "success"
                    )
                    self.database.set_sync_state(
                        mailbox_row_id,
                        scope,
                        window_end=window_end_ms,
                        status=folder_status,
                        error=("; ".join(errors[errors_before:]) or None),
                        extra={
                            **state_extra,
                            "pages": counts.pages_scanned - pages_before,
                            "message_ids": counts.message_ids_seen - ids_before,
                        },
                    )
            else:
                for folder in selected_folders:
                    if folder.search_value is None:
                        raise ValueError(
                            f"邮件文件夹 {folder.provider_folder_id} 缺少可搜索路径"
                        )
                    cursor = start
                    first_window = True
                    while cursor < now:
                        window_end = min(cursor + timedelta(days=1), now)
                        window_start = cursor if first_window else cursor - timedelta(minutes=1)
                        self._sync_window(
                            mailbox_row_id,
                            mailbox_address,
                            folder.search_value,
                            window_start,
                            window_end,
                            seen_ids,
                            counts,
                            errors,
                            page_budget,
                            skip_attachments=skip_attachments,
                        )
                        counts.windows_scanned += 1
                        cursor = window_end
                        first_window = False

            status = "partial" if errors else "success"
            self.database.finish_sync_run(
                run_id,
                status=status,
                error="\n".join(errors) or None,
                **counts.as_dict(),
            )
            return counts
        except Exception as exc:
            self.database.finish_sync_run(
                run_id,
                status="error",
                error=_safe_error(exc),
                **counts.as_dict(),
            )
            raise

    def _sync_window(
        self,
        mailbox_row_id: int,
        mailbox_address: str,
        folder: str,
        start: datetime,
        end: datetime,
        seen_ids: set[str],
        counts: MailSyncCounts,
        errors: list[str],
        page_budget: list[int],
        *,
        skip_attachments: bool,
    ) -> None:
        try:
            self._scan_window_pages(
                mailbox_row_id,
                mailbox_address,
                folder,
                start,
                end,
                seen_ids,
                counts,
                errors,
                page_budget,
                skip_attachments=skip_attachments,
            )
        except MailSyncPartialError:
            duration = end - start
            if duration <= timedelta(minutes=5):
                raise
            midpoint = start + duration / 2
            self._sync_window(
                mailbox_row_id,
                mailbox_address,
                folder,
                start,
                midpoint,
                seen_ids,
                counts,
                errors,
                page_budget,
                skip_attachments=skip_attachments,
            )
            self._sync_window(
                mailbox_row_id,
                mailbox_address,
                folder,
                midpoint - timedelta(seconds=1),
                end,
                seen_ids,
                counts,
                errors,
                page_budget,
                skip_attachments=skip_attachments,
            )

    def _scan_window_pages(
        self,
        mailbox_row_id: int,
        mailbox_address: str,
        folder: str,
        start: datetime,
        end: datetime,
        seen_ids: set[str],
        counts: MailSyncCounts,
        errors: list[str],
        page_budget: list[int],
        *,
        skip_attachments: bool,
    ) -> None:
        page_token: str | None = None
        restarted = False
        while True:
            if page_budget[0] <= 0:
                raise MailSyncPartialError("邮件搜索达到 max_pages 上限")
            try:
                page = self.provider.search_messages(
                    mailbox_address,
                    folder=folder,
                    start_time=start.isoformat(timespec="seconds"),
                    end_time=end.isoformat(timespec="seconds"),
                    page_token=page_token,
                    page_size=15,
                )
            except FeishuAPIError as exc:
                if exc.code == MAIL_SEARCH_TOKEN_EXPIRED and not restarted:
                    page_token = None
                    restarted = True
                    continue
                if exc.code == MAIL_SEARCH_PAGE_LIMIT:
                    raise MailSyncPartialError("邮件搜索分页超过平台上限") from exc
                raise
            page_budget[0] -= 1
            counts.pages_scanned += 1
            notice = str(page.get("notice") or "").strip()
            if notice:
                raise MailSyncPartialError("邮件搜索返回不完整提示")
            message_ids = [str(item) for item in page.get("message_ids") or [] if item]
            if not message_ids:
                message_ids = _message_ids_from_items(page.get("items") or [])
            unique_ids = [
                item for item in dict.fromkeys(message_ids) if item not in seen_ids
            ]
            self._ingest_message_ids(
                mailbox_row_id,
                mailbox_address,
                unique_ids,
                seen_ids,
                counts,
                errors,
                skip_attachments=skip_attachments,
            )
            if not page.get("has_more"):
                return
            next_token = str(page.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise MailSyncPartialError("邮件搜索返回无效 page_token")
            page_token = next_token

    def _scan_folder_pages(
        self,
        mailbox_row_id: int,
        mailbox_address: str,
        folder: MailFolderTarget,
        seen_ids: set[str],
        counts: MailSyncCounts,
        errors: list[str],
        page_budget: list[int],
        *,
        skip_attachments: bool,
    ) -> None:
        page_token: str | None = None
        folder_message_ids: list[str] = []
        folder_seen_ids: set[str] = set()
        while True:
            if page_budget[0] <= 0:
                raise MailSyncPartialError("邮件列表达到 max_pages 上限")
            self._pace_request(
                "_last_list_request_at",
                MAIL_LIST_MIN_INTERVAL_SECONDS,
            )
            page = self.provider.list_message_ids(
                mailbox_address,
                folder_id=(None if folder.list_label_id else folder.provider_folder_id),
                label_id=folder.list_label_id,
                page_token=page_token,
                page_size=20,
            )
            page_budget[0] -= 1
            counts.pages_scanned += 1
            message_ids = [str(item) for item in page.get("message_ids") or [] if item]
            if not message_ids:
                message_ids = _message_ids_from_items(page.get("items") or [])
            for message_id in message_ids:
                if message_id not in folder_seen_ids:
                    folder_seen_ids.add(message_id)
                    folder_message_ids.append(message_id)
            if not page.get("has_more"):
                break
            next_token = str(page.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise MailSyncPartialError("邮件列表返回无效 page_token")
            page_token = next_token

        for offset in range(0, len(folder_message_ids), 20):
            unique_ids = [
                item
                for item in folder_message_ids[offset : offset + 20]
                if item not in seen_ids
            ]
            self._ingest_message_ids(
                mailbox_row_id,
                mailbox_address,
                unique_ids,
                seen_ids,
                counts,
                errors,
                fallback_folder_id=folder.provider_folder_id,
                skip_attachments=skip_attachments,
            )

    def _ingest_message_ids(
        self,
        mailbox_row_id: int,
        mailbox_address: str,
        message_ids: list[str],
        seen_ids: set[str],
        counts: MailSyncCounts,
        errors: list[str],
        *,
        fallback_folder_id: str | None = None,
        skip_attachments: bool,
    ) -> None:
        counts.message_ids_seen += len(message_ids)
        if not message_ids:
            return
        messages, unavailable = self._batch_get_messages_with_retry(
            mailbox_address,
            message_ids,
        )
        if unavailable:
            errors.append(f"{len(unavailable)} 个邮件 ID 未返回详情")
        for source in messages:
            message = dict(source)
            returned_message_id = str(message.get("message_id") or "").strip()
            if fallback_folder_id and not str(message.get("folder_id") or "").strip():
                message["folder_id"] = fallback_folder_id
            self._ingest_message(
                mailbox_row_id,
                mailbox_address,
                message,
                counts,
                errors,
                skip_attachments=skip_attachments,
            )
            if returned_message_id:
                seen_ids.add(returned_message_id)

    def _batch_get_messages_with_retry(
        self,
        mailbox_address: str,
        message_ids: list[str],
    ) -> tuple[list[MailMessage], list[str]]:
        requested = list(dict.fromkeys(message_ids))
        remaining = list(requested)
        messages_by_id: dict[str, MailMessage] = {}
        for attempt in range(len(MAIL_BATCH_RETRY_DELAYS) + 1):
            self._pace_request(
                "_last_batch_request_at",
                MAIL_BATCH_MIN_INTERVAL_SECONDS,
            )
            batch = self.provider.batch_get_messages(
                mailbox_address,
                remaining,
                format="full",
            )
            remaining_set = set(remaining)
            for source in batch.get("messages") or []:
                message = dict(source)
                message_id = str(message.get("message_id") or "").strip()
                if message_id in remaining_set:
                    messages_by_id[message_id] = message
            remaining = [item for item in requested if item not in messages_by_id]
            if not remaining:
                break
            if attempt < len(MAIL_BATCH_RETRY_DELAYS):
                time.sleep(MAIL_BATCH_RETRY_DELAYS[attempt])
        return (
            [messages_by_id[item] for item in requested if item in messages_by_id],
            remaining,
        )

    def _ingest_message(
        self,
        mailbox_row_id: int,
        mailbox_address: str,
        message: MailMessage,
        counts: MailSyncCounts,
        errors: list[str],
        *,
        skip_attachments: bool,
    ) -> None:
        provider_message_id = str(message.get("message_id") or "").strip()
        if not provider_message_id:
            return
        counts.messages_seen += 1

        plain_text = _decode_text(message.get("body_plain_text"))
        body_preview = _decode_text(message.get("body_preview"))
        raw_blob_id: int | None = None
        html_blob_id: int | None = None
        raw_value = message.get("raw")
        if raw_value:
            try:
                raw_bytes = _decode_base64url(str(raw_value))
                raw_blob_id, _, raw_size = self._store_bytes(
                    raw_bytes,
                    media_type="message/rfc822",
                )
                counts.raw_messages_saved += 1
                counts.bytes_downloaded += raw_size
            except (ValueError, MailCapacityError) as exc:
                raw_blob_id = None
                errors.append(
                    f"邮件 {provider_message_id} 的原始 MIME 未保存：{_safe_error(exc)}"
                )
        html_value = message.get("body_html")
        if html_value:
            try:
                html_bytes = _decode_base64url(str(html_value))
                html_blob_id, _, html_size = self._store_bytes(
                    html_bytes,
                    media_type="text/html",
                )
                counts.bytes_downloaded += html_size
            except (ValueError, MailCapacityError) as exc:
                html_blob_id = None
                errors.append(
                    f"邮件 {provider_message_id} 的 HTML 正文未保存：{_safe_error(exc)}"
                )

        recipient_roles = _recipient_roles_present(message)
        recipients = _recipients(message) if recipient_roles else None
        labels = (
            [str(item) for item in message.get("label_ids") or []]
            if "label_ids" in message
            else None
        )
        metadata = _message_metadata(message)
        canonical = json.dumps(
            {
                "subject": message.get("subject") or "",
                "plain": plain_text,
                "metadata": metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        item: dict[str, Any] = {
            **metadata,
            "provider_message_id": provider_message_id,
            "message_id": provider_message_id,
            "content_sha256": hashlib.sha256(canonical).hexdigest(),
            "raw_metadata_json": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        }
        if "body_plain_text" in message or "body_preview" in message:
            item["body_plain_text"] = plain_text or body_preview
        if "body_preview" in message:
            item["body_preview"] = body_preview
        if html_blob_id is not None:
            item["body_html_blob_id"] = html_blob_id
        if raw_blob_id is not None:
            item["raw_blob_id"] = raw_blob_id
        attachments_present = "attachments" in message
        attachments = (
            [dict(value) for value in message.get("attachments") or []]
            if attachments_present
            else []
        )
        normalized_attachments = [_attachment_metadata(value) for value in attachments]
        message_row_id, inserted = self.database.upsert_message(
            mailbox_row_id,
            item,
            recipients=recipients,
            recipient_roles=recipient_roles or None,
            labels=labels,
            attachments=normalized_attachments if attachments_present else None,
        )
        if inserted:
            counts.messages_written += 1

        counts.attachments_seen += len(attachments)
        pending: list[tuple[int, dict[str, Any]]] = []
        for attachment, normalized in zip(attachments, normalized_attachments, strict=True):
            attachment_row_id = self.database.ensure_attachment(message_row_id, normalized)
            if self._attachment_already_stored(attachment_row_id):
                continue
            body = attachment.get("body")
            if body:
                try:
                    payload = _decode_base64url(str(body))
                    self._store_attachment_bytes(attachment_row_id, normalized, payload, counts)
                except (ValueError, MailCapacityError) as exc:
                    self.database.update_attachment(
                        attachment_row_id,
                        status="skipped_capacity" if isinstance(exc, MailCapacityError) else "error",
                        error=_safe_error(exc),
                    )
                    counts.attachments_skipped += 1
                    errors.append(
                        f"邮件 {provider_message_id} 的附件未保存：{_safe_error(exc)}"
                    )
            elif skip_attachments:
                self.database.update_attachment(
                    attachment_row_id,
                    status="metadata_only",
                    error="本次同步跳过附件",
                )
                counts.attachments_skipped += 1
                errors.append(f"邮件 {provider_message_id} 的附件按参数跳过")
            else:
                pending.append((attachment_row_id, normalized))

        if pending:
            self._download_message_attachments(
                mailbox_address,
                provider_message_id,
                pending,
                counts,
                errors,
            )

    def _attachment_already_stored(self, attachment_row_id: int) -> bool:
        item = self.database.get_attachment(attachment_row_id)
        if not item or not item.get("blob_id") or not item.get("relative_path"):
            return False
        if str(item.get("status") or "") not in {"downloaded", "available", "quarantined"}:
            return False
        target = (self.paths.root / str(item["relative_path"])).resolve()
        try:
            target.relative_to(self.paths.root.resolve())
        except ValueError:
            return False
        expected_size = item.get("byte_size")
        expected_digest = str(item.get("sha256") or "")
        return bool(
            expected_size is not None
            and expected_digest
            and _file_matches(target, expected_digest, int(expected_size))
        )

    def _download_message_attachments(
        self,
        mailbox_address: str,
        provider_message_id: str,
        pending: list[tuple[int, dict[str, Any]]],
        counts: MailSyncCounts,
        errors: list[str],
    ) -> None:
        if not self._attachment_downloads_allowed():
            for attachment_row_id, _ in pending:
                self.database.update_attachment(
                    attachment_row_id,
                    status="skipped_capacity",
                    error="磁盘空间已达到邮件附件暂停阈值",
                )
                counts.attachments_skipped += 1
            errors.append(
                f"邮件 {provider_message_id} 的附件因磁盘阈值未下载"
            )
            return
        attachment_ids = [str(item[1].get("attachment_id") or "") for item in pending]
        urls = self._attachment_download_urls(
            mailbox_address,
            provider_message_id,
            [item for item in attachment_ids if item],
        )
        for attachment_row_id, metadata in pending:
            attachment_id = str(metadata.get("attachment_id") or "")
            url = urls.get(attachment_id)
            if not url:
                self.database.update_attachment(
                    attachment_row_id,
                    status="error",
                    error="飞书未返回附件临时下载地址",
                )
                counts.attachments_skipped += 1
                errors.append(
                    f"邮件 {provider_message_id} 的附件 {attachment_id} 未返回下载地址"
                )
                continue
            try:
                blob_id: int | None = None
                digest = ""
                size = 0
                active_url = url
                for attempt in range(2):
                    try:
                        with self.provider.open_download_url(active_url) as response:
                            blob_id, digest, size = self._stream_to_blob(
                                response,
                                media_type=str(
                                    metadata.get("mime_type") or "application/octet-stream"
                                ),
                            )
                        break
                    except FeishuAPIError as exc:
                        if attempt or exc.status not in {401, 403, 404}:
                            raise
                        refreshed = self._attachment_download_urls(
                            mailbox_address,
                            provider_message_id,
                            [attachment_id],
                        )
                        active_url = refreshed.get(attachment_id) or ""
                        if not active_url:
                            raise
                if blob_id is None:
                    raise RuntimeError("附件下载未生成本地 blob")
                status = _attachment_status(str(metadata.get("mime_type") or ""))
                self.database.link_attachment_blob(
                    attachment_row_id,
                    blob_id,
                    sha256=digest,
                    byte_size=size,
                    status=status,
                    error=None,
                )
                counts.attachments_downloaded += 1
                counts.bytes_downloaded += size
            except MailCapacityError as exc:
                self.database.update_attachment(
                    attachment_row_id,
                    status="skipped_capacity",
                    error=_safe_error(exc),
                )
                counts.attachments_skipped += 1
                errors.append(
                    f"邮件 {provider_message_id} 的附件 {attachment_id} 未保存：{_safe_error(exc)}"
                )
            except Exception as exc:
                self.database.update_attachment(
                    attachment_row_id,
                    status="error",
                    error=_safe_error(exc),
                )
                counts.attachments_skipped += 1
                errors.append(
                    f"邮件 {provider_message_id} 的附件 {attachment_id} 下载失败：{_safe_error(exc)}"
                )

    def _attachment_download_urls(
        self,
        mailbox_address: str,
        provider_message_id: str,
        attachment_ids: list[str],
    ) -> dict[str, str]:
        self._pace_request(
            "_last_attachment_url_request_at",
            MAIL_ATTACHMENT_URL_MIN_INTERVAL_SECONDS,
        )
        return self.provider.attachment_download_urls(
            mailbox_address,
            provider_message_id,
            attachment_ids,
        )

    def _pace_request(self, attribute: str, minimum_interval: float) -> None:
        now = time.monotonic()
        elapsed = now - float(getattr(self, attribute))
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)
        setattr(self, attribute, time.monotonic())

    def _store_attachment_bytes(
        self,
        attachment_row_id: int,
        metadata: dict[str, Any],
        payload: bytes,
        counts: MailSyncCounts,
    ) -> None:
        if len(payload) > self.max_attachment_bytes:
            raise MailCapacityError("附件超过单文件容量上限")
        blob_id, digest, size = self._store_bytes(
            payload,
            media_type=str(metadata.get("mime_type") or "application/octet-stream"),
        )
        status = _attachment_status(str(metadata.get("mime_type") or ""))
        self.database.link_attachment_blob(
            attachment_row_id,
            blob_id,
            sha256=digest,
            byte_size=size,
            status=status,
            error=None,
        )
        counts.attachments_downloaded += 1
        counts.bytes_downloaded += size

    def _store_bytes(self, payload: bytes, *, media_type: str) -> tuple[int, str, int]:
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("mail") / "blobs" / digest[:2] / digest
        target = self.paths.root / relative
        existing_blob = self.database.find_blob(digest)
        accounted_size = int(existing_blob["byte_size"]) if existing_blob else 0
        projected_bytes = self._used_bytes - accounted_size + len(payload)
        if projected_bytes > self.max_mail_bytes:
            raise MailCapacityError("邮件通道已达到总容量上限")
        if not _file_matches(target, digest, len(payload)):
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.paths.mail_tmp / f"{digest}-{secrets.token_hex(4)}.part"
            try:
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(payload)
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        self._used_bytes = projected_bytes
        blob_id = self.database.upsert_blob(digest, len(payload), str(relative), media_type=media_type)
        return blob_id, digest, len(payload)

    def _stream_to_blob(self, response: Any, *, media_type: str) -> tuple[int, str, int]:
        headers = getattr(response, "headers", {}) or {}
        content_length_value = headers.get("Content-Length") if hasattr(headers, "get") else None
        content_length = int(content_length_value) if content_length_value else None
        if content_length is not None and content_length > self.max_attachment_bytes:
            raise MailCapacityError("附件声明大小超过单文件容量上限")
        if content_length is not None and self._used_bytes + content_length > self.max_mail_bytes:
            raise MailCapacityError("附件会超过邮件通道总容量上限")

        temporary = self.paths.mail_tmp / f"download-{secrets.token_hex(12)}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                while True:
                    chunk = response.read(MAIL_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_attachment_bytes:
                        raise MailCapacityError("附件实际大小超过单文件容量上限")
                    if self._used_bytes + size > self.max_mail_bytes:
                        raise MailCapacityError("附件实际大小超过邮件通道总容量上限")
                    digest.update(chunk)
                    output.write(chunk)
            hexdigest = digest.hexdigest()
            relative = Path("mail") / "blobs" / hexdigest[:2] / hexdigest
            target = self.paths.root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            existing_blob = self.database.find_blob(hexdigest)
            accounted_size = int(existing_blob["byte_size"]) if existing_blob else 0
            projected_bytes = self._used_bytes - accounted_size + size
            if projected_bytes > self.max_mail_bytes:
                raise MailCapacityError("附件实际大小超过邮件通道总容量上限")
            if _file_matches(target, hexdigest, size):
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            self._used_bytes = projected_bytes
            blob_id = self.database.upsert_blob(
                hexdigest,
                size,
                str(relative),
                media_type=media_type,
            )
            return blob_id, hexdigest, size
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _check_write_capacity(self) -> None:
        usage = shutil.disk_usage(self.paths.root)
        used_ratio = usage.used / usage.total if usage.total else 1.0
        if usage.free < MAIL_HARD_STOP_FREE_BYTES or used_ratio >= 0.97:
            raise MailCapacityError("磁盘已达到邮件写入硬停止阈值")

    def _verify_granted_scopes(self) -> None:
        granted_method = getattr(self.provider, "granted_scopes", None)
        if callable(granted_method):
            granted = {str(scope) for scope in granted_method() if str(scope)}
        else:
            # Compatibility for the production adapter while its public
            # provider surface remains deliberately small. Refreshing first
            # makes the check describe the current user token, not merely a
            # stale access-token cache entry.
            client = getattr(self.provider, "client", None)
            if client is None:
                raise MailAuthorizationError("邮件 provider 无法验证当前 OAuth 授权范围")
            client.user_access_token()
            granted = {str(scope) for scope in client.authorized_scopes() if str(scope)}
        # offline_access is represented operationally by the refresh token and
        # is not consistently echoed in every access-token scope response. The
        # six Mail read scopes are the fields whose omission could corrupt a
        # local update, so those are the hard write gate here. Scheduled sync
        # separately requires a refresh token in CLI readiness checks.
        required = [scope for scope in MAIL_SCOPES if scope != "offline_access"]
        missing = [scope for scope in required if scope not in granted]
        if missing:
            raise MailAuthorizationError(
                "当前 OAuth token 缺少邮件同步权限：" + ", ".join(missing)
            )

    def _attachment_downloads_allowed(self) -> bool:
        usage = shutil.disk_usage(self.paths.root)
        used_ratio = usage.used / usage.total if usage.total else 1.0
        return usage.free >= MAIL_ATTACHMENT_STOP_FREE_BYTES and used_ratio < 0.95


def _deduplicate_folder_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for source in items:
        item = dict(source)
        folder_id = str(item.get("id") or "").strip()
        if not folder_id:
            raise ValueError("飞书邮箱文件夹缺少 id")
        item["id"] = folder_id
        key = folder_id.casefold()
        if key in positions:
            result[positions[key]].update(item)
        else:
            positions[key] = len(result)
            result.append(item)
    return result


def _selected_folder_targets(
    folder_items: Iterable[dict[str, Any]],
    requested: Iterable[str] | None,
    *,
    require_search_paths: bool,
) -> tuple[MailFolderTarget, ...]:
    items = [dict(item) for item in folder_items]
    by_id = {
        str(item.get("id") or "").strip().casefold(): item
        for item in items
        if str(item.get("id") or "").strip()
    }
    system_queries = {
        folder_id.casefold(): search_name
        for folder_id, _name, search_name in SYSTEM_MAIL_FOLDERS
    }
    system_aliases: dict[str, str] = {}
    system_ids_exact: dict[str, str] = {}
    for folder_id, _name, search_name in SYSTEM_MAIL_FOLDERS:
        system_ids_exact[folder_id] = folder_id.casefold()
        system_aliases[folder_id.casefold()] = folder_id.casefold()
        system_aliases[search_name.casefold()] = folder_id.casefold()
    system_aliases["archived"] = "archived"

    path_cache: dict[str, str] = {}

    def search_path(folder_key: str, parents: tuple[str, ...] = ()) -> str:
        if folder_key in system_queries:
            return system_queries[folder_key]
        if folder_key in path_cache:
            return path_cache[folder_key]
        if folder_key in parents:
            raise ValueError("飞书邮箱自定义文件夹父级关系存在循环")
        item = by_id.get(folder_key)
        if item is None:
            raise ValueError("飞书邮箱自定义文件夹的父文件夹不存在")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"飞书邮箱文件夹 {item.get('id')} 缺少名称")
        parent_id = str(item.get("parent_folder_id") or "").strip()
        if not parent_id or parent_id == "0":
            path = name
        else:
            parent_key = parent_id.casefold()
            if parent_key not in by_id:
                raise ValueError(f"飞书邮箱文件夹 {item.get('id')} 的父文件夹不存在")
            path = f"{search_path(parent_key, (*parents, folder_key))}/{name}"
        path_cache[folder_key] = path
        return path

    query_by_id: dict[str, str] = {}
    if require_search_paths or requested is not None:
        for folder_key in by_id:
            try:
                query_by_id[folder_key] = search_path(folder_key)
            except ValueError:
                if require_search_paths:
                    raise
    ordered_keys = [folder_id.casefold() for folder_id, _name, _query in SYSTEM_MAIL_FOLDERS]
    ordered_keys.extend(folder_key for folder_key in by_id if folder_key not in system_queries)

    if requested is None:
        selected_keys = ordered_keys
    else:
        requested_values = [str(value).strip() for value in requested if str(value).strip()]
        if not requested_values:
            raise ValueError("至少需要指定一个非空邮件文件夹")

        by_id_exact: dict[str, str] = {}
        by_query_exact: dict[str, list[str]] = {}
        by_name_exact: dict[str, list[str]] = {}
        by_query: dict[str, list[str]] = {}
        by_name: dict[str, list[str]] = {}
        for folder_key, item in by_id.items():
            provider_id = str(item.get("id") or "").strip()
            by_id_exact[provider_id] = folder_key
            query_value = query_by_id.get(folder_key)
            if query_value is not None:
                by_query_exact.setdefault(query_value, []).append(folder_key)
                by_query.setdefault(query_value.casefold(), []).append(folder_key)
            name = str(item.get("name") or "").strip()
            if name:
                by_name_exact.setdefault(name, []).append(folder_key)
                by_name.setdefault(name.casefold(), []).append(folder_key)

        selected_keys = []
        for value in requested_values:
            lookup = value.casefold()
            folder_key = system_ids_exact.get(value) or by_id_exact.get(value)
            if folder_key is None:
                exact_matches = by_query_exact.get(value) or by_name_exact.get(value) or []
                if len(exact_matches) > 1:
                    raise ValueError(f"邮件文件夹名称不唯一，请改用 folder id：{value}")
                if exact_matches:
                    folder_key = exact_matches[0]
            if folder_key is None:
                folder_key = system_aliases.get(lookup)
            if folder_key is None and lookup in by_id:
                folder_key = lookup
            if folder_key is None:
                matches = by_query.get(lookup) or by_name.get(lookup) or []
                if len(matches) > 1:
                    raise ValueError(f"邮件文件夹名称不唯一，请改用 folder id：{value}")
                if matches:
                    folder_key = matches[0]
            if folder_key is None or folder_key not in by_id:
                raise ValueError(f"未找到邮件文件夹：{value}")
            selected_keys.append(folder_key)

    targets: list[MailFolderTarget] = []
    for folder_key in dict.fromkeys(selected_keys):
        provider_folder_id = str(by_id[folder_key].get("id") or "").strip()
        targets.append(
            MailFolderTarget(
                provider_folder_id=provider_folder_id,
                search_value=query_by_id.get(folder_key),
                list_label_id=("SCHEDULED" if folder_key == "scheduled" else None),
            )
        )
    return tuple(targets)


def _decode_base64url(value: str) -> bytes:
    try:
        encoded = value.strip().encode("ascii")
        if not encoded:
            return b""
        encoded += b"=" * ((4 - len(encoded) % 4) % 4)
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("邮件字段不是有效的 base64url") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(MAIL_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: Path, digest: str, size: int) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == size
            and _file_sha256(path) == digest
        )
    except OSError:
        return False


def _decode_text(value: Any) -> str:
    if not value:
        return ""
    return _decode_base64url(str(value)).decode("utf-8", errors="replace")


def _message_ids_from_items(items: list[Any]) -> list[str]:
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
            continue
        if not isinstance(item, dict):
            continue
        metadata = item.get("meta_data") or item.get("metadata") or item
        if not isinstance(metadata, dict):
            continue
        value = metadata.get("message_biz_id") or metadata.get("message_id") or metadata.get("id")
        if value:
            result.append(str(value))
    return result


def _recipients(message: MailMessage) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    sender = message.get("head_from")
    if isinstance(sender, dict):
        result.append(
            {
                "kind": "from",
                "ordinal": 0,
                "name": str(sender.get("name") or ""),
                "address": str(sender.get("mail_address") or ""),
            }
        )
    for kind in ("to", "cc", "bcc"):
        values = message.get(kind) or []
        for ordinal, value in enumerate(values if isinstance(values, list) else []):
            if not isinstance(value, dict):
                continue
            result.append(
                {
                    "kind": kind,
                    "ordinal": ordinal,
                    "name": str(value.get("name") or ""),
                    "address": str(value.get("mail_address") or ""),
                }
            )
    reply_to = message.get("reply_to")
    reply_values = reply_to if isinstance(reply_to, list) else [reply_to]
    for ordinal, value in enumerate(reply_values):
        if not value:
            continue
        if isinstance(value, dict):
            name = str(value.get("name") or "")
            address = str(value.get("mail_address") or "")
        else:
            name = ""
            address = str(value)
        result.append(
            {
                "kind": "reply_to",
                "ordinal": ordinal,
                "name": name,
                "address": address,
            }
        )
    return result


def _recipient_roles_present(message: MailMessage) -> set[str]:
    aliases = {
        "from": "head_from",
        "to": "to",
        "cc": "cc",
        "bcc": "bcc",
        "reply_to": "reply_to",
    }
    return {role for role, field in aliases.items() if field in message}


def _message_metadata(message: MailMessage) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source, target in (
        ("thread_id", "thread_id"),
        ("smtp_message_id", "smtp_message_id"),
        ("subject", "subject"),
        ("in_reply_to", "in_reply_to"),
        ("priority_type", "priority_type"),
    ):
        if source in message:
            result[target] = str(message.get(source) or "")

    if "head_from" in message:
        sender = message.get("head_from") if isinstance(message.get("head_from"), dict) else {}
        result["sender_name"] = str(sender.get("name") or "")
        result["sender_address"] = str(sender.get("mail_address") or "")
    if "date" in message or "internal_date" in message:
        result["send_date"] = int(message.get("date") or message.get("internal_date") or 0)
        result["received_date"] = int(message.get("internal_date") or message.get("date") or 0)
    if "internal_date" in message:
        result["internal_date"] = int(message.get("internal_date") or 0)
    if "message_state" in message:
        result["message_state"] = int(message.get("message_state") or 0)
    if "folder_id" in message:
        folder_id = str(message.get("folder_id") or "")
        result["current_folder_id"] = folder_id
        result["folder_id"] = folder_id
    if "reply_to" in message:
        result["reply_to"] = _reply_to_text(message.get("reply_to"))
    if "references" in message:
        references = message.get("references") or []
        if isinstance(references, str):
            references = [references]
        result["references_header"] = " ".join(str(item) for item in references)
    if "security_level" in message:
        result["security_level_json"] = json.dumps(
            message.get("security_level") or {}, ensure_ascii=False, separators=(",", ":")
        )
    return result


def _reply_to_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("mail_address") or "")
    if isinstance(value, list):
        return ", ".join(_reply_to_text(item) for item in value)
    return str(value or "")


def _attachment_metadata(item: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(item.get("id") or item.get("attachment_id") or "")
    filename = str(item.get("filename") or "attachment")
    content_type = str(item.get("content_type") or item.get("mime_type") or "").strip()
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "attachment_id": attachment_id,
        "provider_attachment_id": attachment_id,
        "filename": filename,
        "attachment_type": int(item.get("attachment_type") or 1),
        "is_inline": bool(item.get("is_inline")),
        "cid": str(item.get("cid") or ""),
        "content_type": content_type,
        "mime_type": content_type,
        "declared_size": int(item.get("size") or item.get("byte_size") or 0),
    }


def _attachment_status(mime_type: str) -> str:
    lowered = mime_type.casefold().split(";", 1)[0].strip()
    blocked_inline_types = {
        "text/html",
        "image/svg+xml",
        "application/xml",
        "text/xml",
        "application/javascript",
        "text/javascript",
        "application/x-msdownload",
    }
    return "quarantined" if lowered in blocked_inline_types else "available"


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return message[:500] or exc.__class__.__name__
