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
from typing import Any

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
    ) -> None:
        self.database = database
        self.paths = paths
        super().__init__(server_address, ArchiveRequestHandler)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                self._json(self.server.database.status())
            elif parsed.path == "/api/conversations":
                self._json({"items": self.server.database.list_conversations()})
            elif parsed.path == "/api/senders":
                chat_id = _first(query, "chat_id")
                self._json({"items": self.server.database.list_senders(chat_id)})
            elif parsed.path == "/api/messages":
                self._messages(query)
            elif parsed.path == "/api/export":
                self._export(query)
            elif parsed.path.startswith("/api/attachments/"):
                self._attachment(parsed.path)
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
        attachments = self.server.database.attachments_for_messages(
            [str(item["message_id"]) for item in items]
        )
        for item in items:
            item["attachments"] = attachments.get(str(item["message_id"]), [])
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
            batch = self.server.database.query_messages(chat_id=chat_id, limit=500, offset=offset)
            messages.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        filename_base = _download_name(conversation.get("name") or chat_id)
        if export_format == "json":
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

    def _attachment(self, path: str) -> None:
        raw_id = path.removeprefix("/api/attachments/")
        if not raw_id.isdigit():
            raise ValueError("附件 ID 无效")
        attachment = self.server.database.get_attachment(int(raw_id))
        if not attachment or attachment.get("status") != "downloaded" or not attachment.get("local_path"):
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        root = self.server.paths.root.resolve()
        target = (root / str(attachment["local_path"])).resolve()
        if root not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Attachment not found")
            return
        content_type = attachment.get("mime_type") or mimetypes.guess_type(target.name)[0]
        self._bytes(
            target.read_bytes(),
            content_type or "application/octet-stream",
            filename=attachment.get("filename") or target.name,
        )

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
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if filename:
            encoded = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[reader] {self.client_address[0]} {fmt % args}")


def serve(database: ArchiveDatabase, paths: ArchivePaths, host: str, port: int) -> None:
    if not is_loopback_host(host):
        raise ValueError("安全限制：离线阅读器只能监听回环地址 127.0.0.1 或 localhost")
    server = ArchiveHTTPServer((host, port), database, paths)
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
