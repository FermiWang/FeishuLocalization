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
from .mail_database import MailDatabase
from .insights_database import InsightsDatabase
from .reader_auth import ReaderSessionManager


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
LITERAL_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def is_loopback_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    return all(ipaddress.ip_address(address).is_loopback for address in addresses)


def is_literal_loopback_host(host: str) -> bool:
    return host.lower() in LITERAL_LOOPBACK_HOSTS


def _literal_loopback_authority_allowed(authority: str | None, expected_port: int) -> bool:
    if not authority or authority != authority.strip():
        return False
    if any(character.isspace() for character in authority):
        return False
    try:
        parsed = urllib.parse.urlsplit(f"//{authority}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        not host
        or not is_literal_loopback_host(host)
        or port != expected_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return True


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
        mail_database: MailDatabase | None = None,
        mail_sync_controller: Any | None = None,
        mail_session_manager: ReaderSessionManager | None = None,
        mail_sync_schedule: dict[str, Any] | None = None,
        mail_unavailable_reason: str | None = None,
        insights_database: InsightsDatabase | None = None,
        insights_schedule: dict[str, Any] | None = None,
        insights_unavailable_reason: str | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.sync_start = sync_start
        self.sync_schedule = sync_schedule or {"enabled": False}
        self.wiki_sync_start = wiki_sync_start
        self.wiki_sync_schedule = wiki_sync_schedule or {"enabled": False}
        self.mail_database = mail_database
        self.mail_sync_controller = mail_sync_controller
        self.mail_session_manager = mail_session_manager
        self.mail_sync_schedule = mail_sync_schedule or {"enabled": False}
        self.mail_unavailable_reason = mail_unavailable_reason
        self.insights_database = insights_database
        self.insights_schedule = insights_schedule or {"enabled": False}
        self.insights_unavailable_reason = insights_unavailable_reason
        super().__init__(server_address, ArchiveRequestHandler)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_header_allowed():
            self._misdirected_request()
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path.startswith("/api/insights/") and self._insights_api_unavailable():
                return
            if parsed.path.startswith("/api/insights/") and not self._mail_session_valid():
                self._mail_unauthorized()
                return
            if parsed.path.startswith("/api/mail/") and self._mail_api_unavailable():
                return
            if parsed.path.startswith("/api/mail/") and not self._mail_session_valid():
                self._mail_unauthorized()
            elif parsed.path == "/api/insights/status":
                database = self._insights_database()
                if database is not None:
                    self._json({**database.status(), "schedule": self.server.insights_schedule})
            elif parsed.path == "/api/insights/daily":
                database = self._insights_database()
                if database is not None:
                    item = database.latest_report(_first(query, "date"))
                    if item is None:
                        self._json({"error": "尚未生成该日期的每日洞察"}, status=HTTPStatus.NOT_FOUND)
                    else:
                        self._json({"item": item})
            elif parsed.path == "/api/insights/tasks":
                database = self._insights_database()
                if database is not None:
                    self._json(
                        {
                            "items": database.list_tasks(
                                status=_first(query, "status"),
                                limit=int(_first(query, "limit") or 500),
                            )
                        }
                    )
            elif parsed.path == "/api/insights/opportunities":
                database = self._insights_database()
                if database is not None:
                    self._json(
                        {
                            "items": database.list_opportunities(
                                status=_first(query, "status"),
                                limit=int(_first(query, "limit") or 500),
                            )
                        }
                    )
            elif parsed.path == "/api/mail/status":
                self._mail_status(query)
            elif parsed.path == "/api/mail/folders":
                self._mail_folders(query)
            elif parsed.path == "/api/mail/messages":
                self._mail_messages(query)
            elif parsed.path.startswith("/api/mail/messages/"):
                self._mail_message(parsed.path)
            elif parsed.path.startswith("/api/mail/attachments/"):
                self._mail_attachment(parsed.path, query)
            elif parsed.path == "/api/status":
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
        if not self._host_header_allowed():
            self._misdirected_request()
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path.startswith("/api/insights/"):
                if self._insights_api_unavailable():
                    return
                if not self._mail_session_valid():
                    self._mail_unauthorized()
                    return
                if parsed.path.startswith("/api/insights/tasks/") and parsed.path.endswith("/status"):
                    self._insights_task_status(parsed.path)
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if parsed.path.startswith("/api/mail/") and self._mail_api_unavailable():
                return
            if parsed.path == "/api/mail/session":
                self._mail_session()
                return
            if parsed.path.startswith("/api/mail/"):
                if not self._mail_session_valid():
                    self._mail_unauthorized()
                    return
                if parsed.path == "/api/mail/sync":
                    self._mail_sync()
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
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
            if not self._loopback_origin_allowed():
                self._json({"error": "拒绝非本机来源"}, status=HTTPStatus.FORBIDDEN)
                return
            if starter is None:
                self._json({"error": "当前阅读器未启用同步控制"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not starter():
                self._json({"error": "已有同步任务正在运行"}, status=HTTPStatus.CONFLICT)
                return
            self._json({"status": "accepted"}, status=HTTPStatus.ACCEPTED)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception:
            self._json({"error": "启动同步失败"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _mail_database(self) -> MailDatabase | None:
        database = self.server.mail_database
        if database is None:
            self._json(
                {"error": "当前阅读器未启用邮件档案"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return database

    def _insights_database(self) -> InsightsDatabase | None:
        database = self.server.insights_database
        if database is None:
            self._json(
                {"error": "每日洞察暂不可用；聊天、知识库与邮箱阅读仍可使用"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return database

    def _insights_api_unavailable(self) -> bool:
        if self.server.insights_database is not None:
            return False
        self._json(
            {"error": "每日洞察暂不可用；三条源档案仍可使用"},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
        return True

    def _insights_task_status(self, path: str) -> None:
        if self.headers.get("X-Feishu-Archive-Action") != "insights-task-status":
            self._json({"error": "缺少本机任务确认标头"}, status=HTTPStatus.FORBIDDEN)
            return
        if not self._loopback_origin_allowed():
            self._json({"error": "拒绝非本机来源"}, status=HTTPStatus.FORBIDDEN)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5 or not parts[3].isdigit():
            raise ValueError("任务 ID 无效")
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if content_length < 1 or content_length > 4096:
            raise ValueError("任务状态请求体无效")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("任务状态请求必须是 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("任务状态请求必须是 JSON 对象")
        database = self._insights_database()
        if database is None:
            return
        item = database.set_task_status(
            int(parts[3]),
            {
                "status": payload.get("status"),
                "actor_kind": "human",
                "operation_id": payload.get("operation_id"),
                "reason": payload.get("reason"),
            },
        )
        self._json({"item": item})

    def _mail_api_unavailable(self) -> bool:
        if (
            self.server.mail_unavailable_reason is None
            and self.server.mail_database is not None
            and self.server.mail_session_manager is not None
        ):
            return False
        self._json(
            {"error": "邮件档案暂不可用；聊天与知识库仍可使用"},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
        return True

    def _mail_session_valid(self) -> bool:
        manager = self.server.mail_session_manager
        return bool(manager and manager.allows_request(self.headers.get("Cookie")))

    def _host_header_allowed(self) -> bool:
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            return False
        return _literal_loopback_authority_allowed(
            values[0],
            int(self.server.server_address[1]),
        )

    def _misdirected_request(self) -> None:
        self._json(
            {"error": "拒绝非本机或端口不匹配的 Host"},
            status=HTTPStatus.MISDIRECTED_REQUEST,
        )

    def _mail_unauthorized(self) -> None:
        self._json(
            {"error": "邮箱档案已锁定，请使用本机解锁链接重新进入"},
            status=HTTPStatus.UNAUTHORIZED,
        )

    def _mail_session(self) -> None:
        manager = self.server.mail_session_manager
        if manager is None:
            self._json(
                {"error": "当前阅读器未启用邮箱会话"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not self._loopback_origin_allowed():
            self._json({"error": "拒绝非本机来源"}, status=HTTPStatus.FORBIDDEN)
            return
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if content_length < 1:
            raise ValueError("缺少邮箱解锁密钥")
        if content_length > 4096:
            self._json({"error": "请求体过大"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("邮箱解锁请求必须是 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("邮箱解锁请求必须是 JSON 对象")
        presented = payload.get("secret") or payload.get("unlock_token")
        if not isinstance(presented, str):
            raise ValueError("缺少邮箱解锁密钥")
        token = manager.create_session(presented)
        if token is None:
            self._mail_unauthorized()
            return
        self._json(
            {"status": "unlocked", "expires_in": manager.ttl_seconds},
            headers={"Set-Cookie": manager.cookie_value(token)},
        )

    def _mail_status(self, query: dict[str, list[str]]) -> None:
        database = self._mail_database()
        if database is None:
            return
        mailbox = self._mailbox(database, query)
        mailbox_id = int(mailbox["id"]) if mailbox else None
        status = database.status(mailbox_id)
        if mailbox:
            mailbox = _public_mailbox(mailbox)
            mailbox["address"] = mailbox.get("primary_email_address") or ""
        self._json(
            {
                **status,
                "mailbox": mailbox,
                "mailboxes": [_public_mailbox(item) for item in database.list_mailboxes()],
                "schedule": self.server.mail_sync_schedule,
            }
        )

    def _mail_folders(self, query: dict[str, list[str]]) -> None:
        database = self._mail_database()
        if database is None:
            return
        mailbox = self._mailbox(database, query)
        items = database.list_folders(int(mailbox["id"])) if mailbox else []
        self._json(
            {
                "items": [_public_mail_folder(item) for item in items],
                "mailbox": _public_mailbox(mailbox) if mailbox else None,
            }
        )

    def _mail_messages(self, query: dict[str, list[str]]) -> None:
        database = self._mail_database()
        if database is None:
            return
        mailbox = self._mailbox(database, query)
        page = _positive_int(_first(query, "page") or "1", "page", maximum=1_000_000)
        page_size = _positive_int(_first(query, "page_size") or "50", "page_size", maximum=200)
        folder_value = _first(query, "folder_id")
        folder_id: int | str | None = None
        if folder_value:
            folder_id = int(folder_value) if folder_value.isdigit() else folder_value
        items = database.query_messages(
            mailbox_id=int(mailbox["id"]) if mailbox else None,
            query=_first(query, "q"),
            folder_id=folder_id,
            limit=page_size + 1,
            offset=(page - 1) * page_size,
        )
        has_more = len(items) > page_size
        public_items = [_mail_message_value(item) for item in items[:page_size]]
        self._json(
            {
                "items": public_items,
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
            }
        )

    def _mail_message(self, path: str) -> None:
        database = self._mail_database()
        if database is None:
            return
        raw_id = path.removeprefix("/api/mail/messages/")
        if not raw_id.isdigit():
            raise ValueError("邮件 ID 无效")
        item = database.get_message(int(raw_id))
        if item is None:
            self._json({"error": "邮件不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        self._json({"item": _mail_message_value(item)})

    def _mail_attachment(
        self,
        path: str,
        query: dict[str, list[str]],
    ) -> None:
        database = self._mail_database()
        if database is None:
            return
        raw_id = path.removeprefix("/api/mail/attachments/")
        if not raw_id.isdigit():
            raise ValueError("附件 ID 无效")
        attachment = database.get_attachment(int(raw_id))
        if (
            not attachment
            or attachment.get("status") not in {"available", "downloaded", "quarantined"}
            or not attachment.get("relative_path")
        ):
            self._json({"error": "附件不存在或尚未下载"}, status=HTTPStatus.NOT_FOUND)
            return
        quarantined = attachment.get("status") == "quarantined"
        if quarantined and _first(query, "confirm") != "1":
            self._json(
                {
                    "error": (
                        "该附件属于可能执行脚本或主动内容的风险格式；"
                        "请在本机阅读器中明确确认后下载"
                    )
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        root = self.server.paths.root.resolve()
        relative_path = Path(str(attachment["relative_path"]))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            self._json({"error": "附件路径无效"}, status=HTTPStatus.NOT_FOUND)
            return
        target = (root / relative_path).resolve()
        if root not in target.parents or not target.is_file():
            self._json({"error": "附件不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        filename = str(attachment.get("filename") or target.name)
        content_type = (
            attachment.get("content_type")
            or attachment.get("blob_media_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        response_headers = (
            {"X-Feishu-Archive-Warning": "quarantined-attachment"}
            if quarantined
            else None
        )
        self._bytes(
            target.read_bytes(),
            str(content_type),
            filename=filename,
            headers=response_headers,
        )

    def _mail_sync(self) -> None:
        if self.headers.get("X-Feishu-Archive-Action") != "mail-sync":
            self._json({"error": "缺少本机同步确认标头"}, status=HTTPStatus.FORBIDDEN)
            return
        if not self._loopback_origin_allowed():
            self._json({"error": "拒绝非本机来源"}, status=HTTPStatus.FORBIDDEN)
            return
        controller = self.server.mail_sync_controller
        starter = getattr(controller, "request_manual_sync", None)
        if not callable(starter):
            starter = getattr(controller, "start", None)
        if not callable(starter):
            self._json(
                {"error": "当前阅读器未启用邮件同步控制"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not starter():
            self._json({"error": "已有邮件同步任务正在运行"}, status=HTTPStatus.CONFLICT)
            return
        self._json({"status": "accepted"}, status=HTTPStatus.ACCEPTED)

    def _mailbox(
        self,
        database: MailDatabase,
        query: dict[str, list[str]],
    ) -> dict[str, Any] | None:
        raw_id = _first(query, "mailbox_id")
        if raw_id:
            if not raw_id.isdigit():
                raise ValueError("mailbox_id 无效")
            mailbox = database.get_mailbox(int(raw_id))
            if mailbox is None:
                raise ValueError("邮箱账户不存在")
            return mailbox
        mailboxes = database.list_mailboxes()
        return mailboxes[0] if mailboxes else None

    def _loopback_origin_allowed(self) -> bool:
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return True
        if len(origins) != 1:
            return False
        origin = origins[0]
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return bool(
            parsed.scheme == "http"
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
            and _literal_loopback_authority_allowed(
                parsed.netloc,
                int(self.server.server_address[1]),
            )
        )

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
        inline = _inline_wiki_mime(content_type)
        self._bytes(
            target.read_bytes(),
            content_type or "application/octet-stream",
            filename=filename if download or not inline else None,
            allow_same_origin_frame=inline and not download,
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

    def _json(
        self,
        value: Any,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._bytes(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
            headers=headers,
        )

    def _bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        filename: str | None = None,
        allow_same_origin_frame: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        frame_ancestors = "'self'" if allow_same_origin_frame else "'none'"
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
            f"frame-ancestors {frame_ancestors}",
        )
        if filename:
            encoded = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler's default request line contains the raw query.
        # Mail searches can be sensitive, so only log method, path and status.
        path = urllib.parse.urlparse(self.path).path
        status = str(args[1]) if len(args) > 1 else "-"
        print(f"[reader] {self.client_address[0]} {self.command} {path} {status}")


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
    mail_database: MailDatabase | None = None,
    mail_sync_controller: Any | None = None,
    mail_session_manager: ReaderSessionManager | None = None,
    mail_sync_schedule: dict[str, Any] | None = None,
    mail_unavailable_reason: str | None = None,
    insights_database: InsightsDatabase | None = None,
    insights_schedule: dict[str, Any] | None = None,
    insights_unavailable_reason: str | None = None,
) -> None:
    if not is_literal_loopback_host(host) or not is_loopback_host(host):
        raise ValueError("安全限制：离线阅读器只能监听回环地址 127.0.0.1 或 localhost")
    server = ArchiveHTTPServer(
        (host, port),
        database,
        paths,
        sync_start=sync_start,
        sync_schedule=sync_schedule,
        wiki_sync_start=wiki_sync_start,
        wiki_sync_schedule=wiki_sync_schedule,
        mail_database=mail_database,
        mail_sync_controller=mail_sync_controller,
        mail_session_manager=mail_session_manager,
        mail_sync_schedule=mail_sync_schedule,
        mail_unavailable_reason=mail_unavailable_reason,
        insights_database=insights_database,
        insights_schedule=insights_schedule,
        insights_unavailable_reason=insights_unavailable_reason,
    )
    try:
        if not ipaddress.ip_address(str(server.server_address[0])).is_loopback:
            raise ValueError("安全限制：阅读器实际监听地址不是回环地址")
    except Exception:
        server.server_close()
        raise
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


def _positive_int(value: str, name: str, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if parsed < 1 or parsed > maximum:
        raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
    return parsed


def _mail_message_value(item: dict[str, Any]) -> dict[str, Any]:
    value = dict(item)
    for key in (
        "raw_json",
        "security_level_json",
        "source_hash",
        "body_html_blob_id",
        "raw_blob_id",
    ):
        value.pop(key, None)
    for relation in ("recipients", "labels", "folders", "attachments"):
        if not isinstance(value.get(relation), list):
            continue
        public_items: list[dict[str, Any]] = []
        for raw in value[relation]:
            if not isinstance(raw, dict):
                continue
            public = dict(raw)
            for key in ("raw_json", "relative_path", "blob_id", "sha256", "provider_id"):
                public.pop(key, None)
            public_items.append(public)
        value[relation] = public_items
    value.setdefault("snippet", value.get("excerpt") or value.get("body_plain_text") or "")
    value.setdefault("sent_at", value.get("send_date"))
    value.setdefault("received_at", value.get("received_date"))
    return value


def _public_mailbox(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "provider",
        "provider_mailbox_id",
        "primary_email_address",
        "display_name",
        "status",
        "last_seen_at",
        "last_synced_at",
        "error",
    }
    return {key: value for key, value in item.items() if key in allowed}


def _public_mail_folder(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "mailbox_id",
        "provider_folder_id",
        "name",
        "folder_type",
        "parent_provider_folder_id",
        "unread_count",
        "total_count",
        "message_count",
        "status",
        "last_seen_at",
    }
    return {key: value for key, value in item.items() if key in allowed}


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
