from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Iterator, cast

from .feishu import RESOURCE_DOWNLOAD_TIMEOUT, FeishuAPIError, FeishuClient
from .mail_provider import (
    BatchGetResult,
    BinaryResponse,
    MailFolder,
    MailMessage,
    MailMessageFormat,
    MailMessagePage,
    MailTime,
    MailboxProfile,
)


MAIL_SEARCH_PAGE_SIZE = 15
MAIL_LIST_PAGE_SIZE = 20
MAIL_BATCH_GET_SIZE = 20
MAIL_ATTACHMENT_URL_BATCH_SIZE = 20
MAIL_MESSAGE_FORMATS = {"metadata", "plain_text_full", "full"}

_SEARCH_FOLDER_NAMES = {
    "INBOX": "inbox",
    "SENT": "sent",
    "DRAFT": "draft",
    "TRASH": "trash",
    "SPAM": "spam",
    "ARCHIVED": "archive",
    "SCHEDULED": "scheduled",
}


class FeishuMailProvider:
    """Read-only Feishu Mail v1 adapter using the OAuth user's access token."""

    def __init__(self, client: FeishuClient) -> None:
        self.client = client

    def granted_scopes(self) -> set[str]:
        """Return scopes for the current mail token after refreshing if needed."""

        self.client.user_access_token()
        return self.client.authorized_scopes()

    def profile(self, mailbox_id: str = "me") -> MailboxProfile:
        data = self._data(
            self._request("GET", self._mailbox_path(mailbox_id, "profile"))
        )
        nested = data.get("user_mailbox")
        source = nested if isinstance(nested, Mapping) else data
        primary_email = str(source.get("primary_email_address") or "").strip()
        if not primary_email:
            primary_email = str(data.get("primary_email_address") or "").strip()
        if not primary_email:
            raise FeishuAPIError("飞书邮箱 profile 响应中没有 primary_email_address")
        result: dict[str, Any] = dict(source)
        result["primary_email_address"] = primary_email
        result.setdefault("mailbox_id", primary_email)
        return cast(MailboxProfile, result)

    def list_folders(self, mailbox_id: str = "me") -> list[MailFolder]:
        data = self._data(
            self._request("GET", self._mailbox_path(mailbox_id, "folders"))
        )
        raw_items = data.get("items") or []
        if not isinstance(raw_items, list):
            raise FeishuAPIError("飞书邮箱 folders 响应中的 items 不是列表")
        folders: list[MailFolder] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise FeishuAPIError("飞书邮箱 folders 响应包含无效项目")
            folder_id = str(raw.get("id") or "").strip()
            if not folder_id:
                raise FeishuAPIError("飞书邮箱 folders 响应包含没有 id 的项目")
            folder = dict(raw)
            folder["id"] = folder_id
            folder["name"] = str(raw.get("name") or "")
            folders.append(cast(MailFolder, folder))
        return folders

    def search_messages(
        self,
        mailbox_id: str = "me",
        *,
        folder: str | None = None,
        start_time: MailTime | None = None,
        end_time: MailTime | None = None,
        page_token: str | None = None,
        page_size: int = MAIL_SEARCH_PAGE_SIZE,
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
        if not 1 <= page_size <= MAIL_SEARCH_PAGE_SIZE:
            raise ValueError(f"page_size 必须在 1 到 {MAIL_SEARCH_PAGE_SIZE} 之间")
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        filter_body: dict[str, Any] = {}
        if folder and folder.strip():
            filter_body["folder"] = [self._search_folder(folder)]
        if label and label.strip():
            filter_body["label"] = [label.strip()]
        self._add_address_filter(filter_body, "from", from_addresses)
        self._add_address_filter(filter_body, "to", to_addresses)
        self._add_address_filter(filter_body, "cc", cc_addresses)
        self._add_address_filter(filter_body, "bcc", bcc_addresses)
        if subject and subject.strip():
            filter_body["subject"] = subject.strip()
        if has_attachment is not None:
            filter_body["has_attachment"] = bool(has_attachment)
        if is_unread is not None:
            filter_body["is_unread"] = bool(is_unread)

        if (start_time is None) != (end_time is None):
            raise ValueError("邮件搜索时间范围必须同时提供 start_time 和 end_time")
        create_time: dict[str, str] = {}
        if start_time is not None:
            create_time["start_time"] = self._mail_time(start_time)
        if end_time is not None:
            create_time["end_time"] = self._mail_time(end_time)
        if create_time:
            filter_body["create_time"] = create_time

        payload: dict[str, Any] = {}
        if query and query.strip():
            normalized_query = query.strip()
            if len(normalized_query) > 50:
                raise ValueError("query 不能超过 50 个字符")
            payload["query"] = normalized_query
        if filter_body:
            payload["filter"] = filter_body
        if not payload:
            raise ValueError("搜索邮件至少需要一个 query 或 filter 条件")

        data = self._data(
            self._request(
                "POST",
                self._mailbox_path(mailbox_id, "search"),
                params=params,
                payload=payload,
            )
        )
        return self._page(data, self._search_message_id)

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
                raise FeishuAPIError("邮件搜索分页返回了无效 page_token")
            token = next_token

    def list_message_ids(
        self,
        mailbox_id: str = "me",
        *,
        folder_id: str | None = None,
        label_id: str | None = None,
        only_unread: bool | None = None,
        page_token: str | None = None,
        page_size: int = MAIL_LIST_PAGE_SIZE,
    ) -> MailMessagePage:
        if not 1 <= page_size <= MAIL_LIST_PAGE_SIZE:
            raise ValueError(f"page_size 必须在 1 到 {MAIL_LIST_PAGE_SIZE} 之间")
        folder_id = str(folder_id or "").strip() or None
        label_id = str(label_id or "").strip() or None
        if folder_id and label_id:
            raise ValueError("folder_id 和 label_id 不能同时设置")
        if not folder_id and not label_id:
            folder_id = "INBOX"

        params: dict[str, Any] = {"page_size": page_size}
        if folder_id:
            params["folder_id"] = folder_id.strip()
        if label_id:
            params["label_id"] = label_id.strip()
        if only_unread:
            params["only_unread"] = True
        if page_token:
            params["page_token"] = page_token

        data = self._data(
            self._request(
                "GET",
                self._mailbox_path(mailbox_id, "messages"),
                params=params,
            )
        )
        return self._page(data, self._listed_message_id)

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
                raise FeishuAPIError("邮件列表分页返回了无效 page_token")
            token = next_token

    def batch_get_messages(
        self,
        mailbox_id: str,
        message_ids: list[str],
        *,
        format: MailMessageFormat = "full",
    ) -> BatchGetResult:
        if format not in MAIL_MESSAGE_FORMATS:
            raise ValueError("format 必须是 metadata、plain_text_full 或 full")
        requested = self._ids(message_ids, "message_id")
        unique = list(dict.fromkeys(requested))
        by_id: dict[str, MailMessage] = {}
        path = self._mailbox_path(mailbox_id, "messages", "batch_get")
        for batch in self._chunks(unique, MAIL_BATCH_GET_SIZE):
            data = self._data(
                self._request(
                    "POST",
                    path,
                    payload={"format": format, "message_ids": batch},
                )
            )
            raw_messages = data.get("messages") or []
            if not isinstance(raw_messages, list):
                raise FeishuAPIError("飞书邮箱 batch_get 响应中的 messages 不是列表")
            for raw in raw_messages:
                if not isinstance(raw, Mapping):
                    raise FeishuAPIError("飞书邮箱 batch_get 响应包含无效邮件")
                message_id = str(raw.get("message_id") or "").strip()
                if not message_id:
                    raise FeishuAPIError("飞书邮箱 batch_get 响应包含没有 message_id 的邮件")
                by_id[message_id] = cast(MailMessage, dict(raw))

        return {
            "messages": [by_id[item] for item in requested if item in by_id],
            "unavailable_message_ids": [item for item in requested if item not in by_id],
        }

    def attachment_download_urls(
        self,
        mailbox_id: str,
        message_id: str,
        attachment_ids: list[str],
    ) -> dict[str, str]:
        requested = self._ids(attachment_ids, "attachment_id")
        unique = list(dict.fromkeys(requested))
        urls: dict[str, str] = {}
        base_path = self._mailbox_path(
            mailbox_id,
            "messages",
            message_id,
            "attachments",
            "download_url",
        )
        for batch in self._chunks(unique, MAIL_ATTACHMENT_URL_BATCH_SIZE):
            query = urllib.parse.urlencode(
                [("attachment_ids", attachment_id) for attachment_id in batch]
            )
            data = self._data(self._request("GET", f"{base_path}?{query}"))
            raw_urls = data.get("download_urls") or []
            if not isinstance(raw_urls, list):
                raise FeishuAPIError(
                    "飞书邮箱附件 URL 响应中的 download_urls 不是列表"
                )
            for raw in raw_urls:
                if not isinstance(raw, Mapping):
                    raise FeishuAPIError("飞书邮箱附件 URL 响应包含无效项目")
                attachment_id = str(raw.get("attachment_id") or "").strip()
                download_url = str(raw.get("download_url") or "").strip()
                if not attachment_id or not download_url:
                    continue
                self._validate_download_url(download_url, resolve=False)
                urls[attachment_id] = download_url
        return {item: urls[item] for item in requested if item in urls}

    def open_download_url(self, url: str) -> BinaryResponse:
        """Open a short-lived attachment URL without sending OAuth credentials.

        Every URL, including each redirect target, must be HTTPS on port 443 and
        resolve only to globally routable addresses. The signed URL is therefore
        never turned into a bearer-authenticated request or a local-network SSRF.
        """

        self._validate_download_url(url, resolve=True)
        redirect_handler = _SafeHTTPSRedirectHandler(
            lambda target: self._validate_download_url(target, resolve=True)
        )
        opener = urllib.request.build_opener(
            # Signed attachment URLs are credentials in their own right. Do not
            # leak them to HTTP(S)_PROXY inherited by a LaunchAgent.
            urllib.request.ProxyHandler({}),
            redirect_handler,
            urllib.request.HTTPSHandler(context=self.client.ssl_context),
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/octet-stream"},
        )
        try:
            response = opener.open(
                request,
                timeout=max(self.client.timeout, RESOURCE_DOWNLOAD_TIMEOUT),
            )
            final_url = response.geturl()
            self._validate_download_url(final_url, resolve=True)
            self._validate_response_peer(response)
            return cast(BinaryResponse, response)
        except urllib.error.HTTPError as exc:
            raise self.client._http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise FeishuAPIError(f"邮件附件请求失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise FeishuAPIError("邮件附件请求失败：请求超时") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Explicit auth=True is important: Mail is a per-user archive lane and
        # must never silently fall back to tenant_access_token semantics.
        return self.client._json_request(
            method,
            path,
            params=params,
            payload=payload,
            auth=True,
        )

    @staticmethod
    def _data(result: dict[str, Any]) -> dict[str, Any]:
        data = result.get("data")
        if not isinstance(data, dict):
            raise FeishuAPIError("飞书邮箱 API 响应中的 data 不是对象")
        return data

    @classmethod
    def _mailbox_path(cls, mailbox_id: str, *segments: str) -> str:
        parts = [cls._segment(mailbox_id, "mailbox_id")]
        parts.extend(cls._segment(item, "path segment") for item in segments)
        return "/mail/v1/user_mailboxes/" + "/".join(parts)

    @staticmethod
    def _segment(value: str, name: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return urllib.parse.quote(normalized, safe="")

    @staticmethod
    def _search_folder(folder: str) -> str:
        normalized = folder.strip()
        return _SEARCH_FOLDER_NAMES.get(normalized.upper(), normalized)

    @staticmethod
    def _mail_time(value: MailTime) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("邮件搜索时间必须包含时区")
            return value.isoformat(timespec="seconds")
        if isinstance(value, (int, float)):
            seconds = float(value)
            if abs(seconds) >= 100_000_000_000:
                seconds /= 1000
            converted = datetime.fromtimestamp(seconds, tz=timezone.utc)
            return converted.isoformat(timespec="seconds").replace("+00:00", "Z")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("邮件搜索时间不能为空")
        return normalized

    @staticmethod
    def _add_address_filter(
        target: dict[str, Any],
        key: str,
        values: Sequence[str] | None,
    ) -> None:
        normalized = [str(value).strip() for value in (values or []) if str(value).strip()]
        if normalized:
            target[key] = normalized

    @staticmethod
    def _search_message_id(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, Mapping):
            return ""
        metadata = item.get("meta_data")
        if isinstance(metadata, Mapping):
            value = metadata.get("message_biz_id") or metadata.get("message_id")
            if value:
                return str(value).strip()
        return str(item.get("message_id") or item.get("id") or "").strip()

    @staticmethod
    def _listed_message_id(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, Mapping):
            return str(item.get("message_id") or item.get("id") or "").strip()
        return ""

    @staticmethod
    def _page(
        data: dict[str, Any],
        id_from_item: Callable[[Any], str],
    ) -> MailMessagePage:
        raw_items = data.get("items") or []
        if not isinstance(raw_items, list):
            raise FeishuAPIError("飞书邮箱分页响应中的 items 不是列表")
        message_ids = [message_id for item in raw_items if (message_id := id_from_item(item))]
        has_more = bool(data.get("has_more"))
        token = str(data.get("page_token") or "").strip()
        result: MailMessagePage = {
            "items": list(raw_items),
            "message_ids": message_ids,
            "has_more": has_more,
        }
        if token:
            result["page_token"] = token
        notice = str(data.get("notice") or "").strip()
        if notice:
            result["notice"] = notice
        return result

    @staticmethod
    def _ids(values: Sequence[str], name: str) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
        for start in range(0, len(values), size):
            yield values[start : start + size]

    @staticmethod
    def _validate_download_url(url: str, *, resolve: bool) -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise FeishuAPIError("邮件附件下载 URL 无效") from exc
        if parsed.scheme.lower() != "https":
            raise FeishuAPIError("邮件附件下载 URL 必须使用 HTTPS")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise FeishuAPIError("邮件附件下载 URL 的主机无效")
        if port not in (None, 443):
            raise FeishuAPIError("邮件附件下载 URL 只能使用 HTTPS 443 端口")

        try:
            host = parsed.hostname.encode("idna").decode("ascii").rstrip(".").lower()
        except UnicodeError as exc:
            raise FeishuAPIError("邮件附件下载 URL 的主机无效") from exc
        if (
            not host
            or host == "localhost"
            or host.endswith(".localhost")
            or host.endswith(".local")
            or host.endswith(".internal")
        ):
            raise FeishuAPIError("拒绝访问本机或私网邮件附件 URL")

        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not literal.is_global:
                raise FeishuAPIError("拒绝访问本机或私网邮件附件 URL")
            return
        if not resolve:
            return

        try:
            addresses = socket.getaddrinfo(
                host,
                port or 443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise FeishuAPIError(f"邮件附件下载域名解析失败：{exc}") from exc
        if not addresses:
            raise FeishuAPIError("邮件附件下载域名没有可用地址")
        for address in addresses:
            raw_ip = str(address[4][0]).split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                raise FeishuAPIError("邮件附件下载域名解析结果无效") from exc
            if not resolved_ip.is_global:
                raise FeishuAPIError("拒绝访问解析到本机或私网的邮件附件 URL")

    @staticmethod
    def _validate_response_peer(response: Any) -> None:
        """Verify the address actually reached, closing the DNS rebinding gap."""

        peer_ip = getattr(response, "peer_ip", None)
        if not peer_ip:
            try:
                peer_ip = response.fp.raw._sock.getpeername()[0]
            except (AttributeError, IndexError, OSError, TypeError) as exc:
                response.close()
                raise FeishuAPIError("无法验证邮件附件连接的远端地址") from exc
        try:
            address = ipaddress.ip_address(str(peer_ip).split("%", 1)[0])
        except ValueError as exc:
            response.close()
            raise FeishuAPIError("邮件附件连接的远端地址无效") from exc
        if not address.is_global:
            response.close()
            raise FeishuAPIError("拒绝访问连接到本机或私网的邮件附件 URL")


class _SafeHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self.validator(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected
