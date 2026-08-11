from __future__ import annotations

import copy
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Literal, NotRequired, Protocol, TypedDict, runtime_checkable

from .config import MAIL_SCOPES


MailMessageFormat = Literal["metadata", "plain_text_full", "full"]
MailTime = str | int | float | datetime


class MailAddress(TypedDict, total=False):
    mail_address: str
    name: str


class MailAttachment(TypedDict, total=False):
    id: str
    filename: str
    content_type: str
    attachment_type: int
    is_inline: bool
    cid: str
    size: int
    body: str


class MailboxProfile(TypedDict):
    primary_email_address: str
    mailbox_id: NotRequired[str]
    display_name: NotRequired[str]
    status: NotRequired[int | str]


class MailFolder(TypedDict):
    id: str
    name: str
    parent_folder_id: NotRequired[str]
    folder_type: NotRequired[int]
    unread_message_count: NotRequired[int]
    unread_thread_count: NotRequired[int]


class MailMessage(TypedDict, total=False):
    message_id: str
    thread_id: str
    smtp_message_id: str
    in_reply_to: str
    references: list[str] | str
    subject: str
    head_from: MailAddress
    to: list[MailAddress]
    cc: list[MailAddress]
    bcc: list[MailAddress]
    reply_to: str | MailAddress | list[MailAddress]
    date: str | int
    internal_date: str | int
    message_state: int
    folder_id: str
    label_ids: list[str]
    priority_type: str | int
    security_level: dict[str, Any]
    body_preview: str
    body_plain_text: str
    body_html: str
    body_calendar: str
    raw: str
    attachments: list[MailAttachment]


class MailMessagePage(TypedDict):
    items: list[Any]
    message_ids: list[str]
    has_more: bool
    page_token: NotRequired[str]
    notice: NotRequired[str]


class BatchGetResult(TypedDict):
    messages: list[MailMessage]
    unavailable_message_ids: list[str]


class BinaryResponse(Protocol):
    headers: Any

    def read(self, amt: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...

    def __enter__(self) -> BinaryResponse: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


@runtime_checkable
class MailProvider(Protocol):
    """Read-only provider contract consumed by the mail sync lane."""

    def granted_scopes(self) -> set[str]: ...

    def profile(self, mailbox_id: str = "me") -> MailboxProfile: ...

    def list_folders(self, mailbox_id: str = "me") -> list[MailFolder]: ...

    def search_messages(
        self,
        mailbox_id: str = "me",
        *,
        folder: str | None = None,
        start_time: MailTime | None = None,
        end_time: MailTime | None = None,
        page_token: str | None = None,
        page_size: int = 15,
        query: str | None = None,
        label: str | None = None,
        from_addresses: list[str] | None = None,
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        bcc_addresses: list[str] | None = None,
        subject: str | None = None,
        has_attachment: bool | None = None,
        is_unread: bool | None = None,
    ) -> MailMessagePage: ...

    def iter_search_pages(
        self,
        mailbox_id: str = "me",
        **kwargs: Any,
    ) -> Iterator[MailMessagePage]: ...

    def list_message_ids(
        self,
        mailbox_id: str = "me",
        *,
        folder_id: str | None = None,
        label_id: str | None = None,
        only_unread: bool | None = None,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> MailMessagePage: ...

    def iter_message_id_pages(
        self,
        mailbox_id: str = "me",
        **kwargs: Any,
    ) -> Iterator[MailMessagePage]: ...

    def batch_get_messages(
        self,
        mailbox_id: str,
        message_ids: list[str],
        *,
        format: MailMessageFormat = "full",
    ) -> BatchGetResult: ...

    def attachment_download_urls(
        self,
        mailbox_id: str,
        message_id: str,
        attachment_ids: list[str],
    ) -> dict[str, str]: ...

    def open_download_url(self, url: str) -> BinaryResponse: ...


@dataclass
class FakeMailProvider:
    """In-memory provider for deterministic sync and failure-path tests.

    The fake intentionally returns copies so production code cannot mutate the
    configured fixtures. Pagination tokens are opaque to callers even though
    the fake implements them as decimal offsets.
    """

    profile_value: MailboxProfile = field(
        default_factory=lambda: {
            "primary_email_address": "archive@example.com",
            "mailbox_id": "archive@example.com",
        }
    )
    folders: list[MailFolder] = field(default_factory=list)
    messages: dict[str, MailMessage] = field(default_factory=dict)
    search_message_ids: list[str] | None = None
    listed_message_ids: dict[str, list[str]] = field(default_factory=dict)
    attachment_urls: dict[str, dict[str, str]] = field(default_factory=dict)
    downloads: dict[str, bytes] = field(default_factory=dict)
    granted_scope_values: set[str] = field(default_factory=lambda: set(MAIL_SCOPES))
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def granted_scopes(self) -> set[str]:
        self.calls.append(("granted_scopes", {}))
        return set(self.granted_scope_values)

    def profile(self, mailbox_id: str = "me") -> MailboxProfile:
        self.calls.append(("profile", {"mailbox_id": mailbox_id}))
        return copy.deepcopy(self.profile_value)

    def list_folders(self, mailbox_id: str = "me") -> list[MailFolder]:
        self.calls.append(("list_folders", {"mailbox_id": mailbox_id}))
        return copy.deepcopy(self.folders)

    def search_messages(
        self,
        mailbox_id: str = "me",
        *,
        folder: str | None = None,
        start_time: MailTime | None = None,
        end_time: MailTime | None = None,
        page_token: str | None = None,
        page_size: int = 15,
        query: str | None = None,
        label: str | None = None,
        from_addresses: list[str] | None = None,
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        bcc_addresses: list[str] | None = None,
        subject: str | None = None,
        has_attachment: bool | None = None,
        is_unread: bool | None = None,
    ) -> MailMessagePage:
        arguments: dict[str, Any] = {
            "mailbox_id": mailbox_id,
            "folder": folder,
            "start_time": start_time,
            "end_time": end_time,
            "page_token": page_token,
            "page_size": page_size,
            "query": query,
            "label": label,
            "from_addresses": from_addresses,
            "to_addresses": to_addresses,
            "cc_addresses": cc_addresses,
            "bcc_addresses": bcc_addresses,
            "subject": subject,
            "has_attachment": has_attachment,
            "is_unread": is_unread,
        }
        self.calls.append(("search_messages", arguments))
        ids = list(
            self.search_message_ids
            if self.search_message_ids is not None
            else self.messages.keys()
        )
        if folder:
            normalized = _fake_folder_id(folder)
            ids = [
                message_id
                for message_id in ids
                if _fake_folder_id(str(self.messages.get(message_id, {}).get("folder_id") or ""))
                == normalized
            ]
        page_ids, next_token = _fake_page(ids, page_token, page_size)
        items: list[dict[str, Any]] = []
        for message_id in page_ids:
            message = self.messages.get(message_id, {})
            items.append(
                {
                    "meta_data": {
                        "message_biz_id": message_id,
                        "thread_id": message.get("thread_id", ""),
                        "title": message.get("subject", ""),
                        "create_time": message.get("internal_date", ""),
                    }
                }
            )
        result: MailMessagePage = {
            "items": items,
            "message_ids": page_ids,
            "has_more": next_token is not None,
        }
        if next_token is not None:
            result["page_token"] = next_token
        return result

    def iter_search_pages(
        self,
        mailbox_id: str = "me",
        **kwargs: Any,
    ) -> Iterator[MailMessagePage]:
        token = kwargs.pop("page_token", None)
        while True:
            page = self.search_messages(mailbox_id, page_token=token, **kwargs)
            yield page
            if not page["has_more"]:
                return
            next_token = page.get("page_token")
            if not next_token or next_token == token:
                raise RuntimeError("fake mail search returned an invalid page token")
            token = next_token

    def list_message_ids(
        self,
        mailbox_id: str = "me",
        *,
        folder_id: str | None = None,
        label_id: str | None = None,
        only_unread: bool | None = None,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> MailMessagePage:
        folder_id = folder_id or (None if label_id else "INBOX")
        self.calls.append(
            (
                "list_message_ids",
                {
                    "mailbox_id": mailbox_id,
                    "folder_id": folder_id,
                    "label_id": label_id,
                    "only_unread": only_unread,
                    "page_token": page_token,
                    "page_size": page_size,
                },
            )
        )
        if folder_id in self.listed_message_ids:
            ids = list(self.listed_message_ids[folder_id])
        else:
            ids = list(self.messages)
            if folder_id:
                wanted = _fake_folder_id(folder_id)
                ids = [
                    message_id
                    for message_id in ids
                    if _fake_folder_id(str(self.messages[message_id].get("folder_id") or ""))
                    == wanted
                ]
            if label_id:
                ids = [
                    message_id
                    for message_id in ids
                    if label_id in (self.messages[message_id].get("label_ids") or [])
                ]
            if only_unread:
                ids = [
                    message_id
                    for message_id in ids
                    if bool(self.messages[message_id].get("is_unread"))
                    or "UNREAD" in (self.messages[message_id].get("label_ids") or [])
                ]
        page_ids, next_token = _fake_page(ids, page_token, page_size)
        result: MailMessagePage = {
            "items": list(page_ids),
            "message_ids": list(page_ids),
            "has_more": next_token is not None,
        }
        if next_token is not None:
            result["page_token"] = next_token
        return result

    def iter_message_id_pages(
        self,
        mailbox_id: str = "me",
        **kwargs: Any,
    ) -> Iterator[MailMessagePage]:
        token = kwargs.pop("page_token", None)
        while True:
            page = self.list_message_ids(mailbox_id, page_token=token, **kwargs)
            yield page
            if not page["has_more"]:
                return
            next_token = page.get("page_token")
            if not next_token or next_token == token:
                raise RuntimeError("fake mail list returned an invalid page token")
            token = next_token

    def batch_get_messages(
        self,
        mailbox_id: str,
        message_ids: list[str],
        *,
        format: MailMessageFormat = "full",
    ) -> BatchGetResult:
        self.calls.append(
            (
                "batch_get_messages",
                {
                    "mailbox_id": mailbox_id,
                    "message_ids": list(message_ids),
                    "format": format,
                },
            )
        )
        return {
            "messages": [
                copy.deepcopy(self.messages[item])
                for item in message_ids
                if item in self.messages
            ],
            "unavailable_message_ids": [item for item in message_ids if item not in self.messages],
        }

    def attachment_download_urls(
        self,
        mailbox_id: str,
        message_id: str,
        attachment_ids: list[str],
    ) -> dict[str, str]:
        self.calls.append(
            (
                "attachment_download_urls",
                {
                    "mailbox_id": mailbox_id,
                    "message_id": message_id,
                    "attachment_ids": list(attachment_ids),
                },
            )
        )
        configured = self.attachment_urls.get(message_id, {})
        return {item: configured[item] for item in attachment_ids if item in configured}

    def open_download_url(self, url: str) -> BinaryResponse:
        self.calls.append(("open_download_url", {"url": url}))
        if url not in self.downloads:
            raise FileNotFoundError(url)
        return io.BytesIO(self.downloads[url])  # type: ignore[return-value]


def _fake_folder_id(value: str) -> str:
    aliases = {
        "inbox": "INBOX",
        "sent": "SENT",
        "draft": "DRAFT",
        "trash": "TRASH",
        "spam": "SPAM",
        "archive": "ARCHIVED",
        "archived": "ARCHIVED",
    }
    stripped = value.strip()
    return aliases.get(stripped.casefold(), stripped)


def _fake_page(
    items: list[str],
    page_token: str | None,
    page_size: int,
) -> tuple[list[str], str | None]:
    if page_size < 1:
        raise ValueError("page_size must be positive")
    try:
        offset = int(page_token or 0)
    except ValueError as exc:
        raise ValueError("invalid fake page token") from exc
    if offset < 0:
        raise ValueError("invalid fake page token")
    page = items[offset : offset + page_size]
    next_offset = offset + len(page)
    return page, str(next_offset) if next_offset < len(items) else None
