from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_OAUTH_PORT,
    DEFAULT_READER_PORT,
    FeishuAppConfig,
    archive_paths,
)
from .database import ArchiveDatabase
from .demo import seed_demo
from .feishu import FeishuAPIError, FeishuClient
from .keychain import KeychainError, KeychainStore
from .sync import ArchiveSyncer
from .web import is_loopback_host, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-archive",
        description="通过飞书官方接口建立本机离线消息档案",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--archive-dir",
        help="档案目录，默认 ~/Library/Application Support/Feishu Archive",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化本地档案库")
    subparsers.add_parser("demo", help="写入三个 PoC 示例会话")

    auth = subparsers.add_parser("auth", help="通过浏览器完成飞书用户 OAuth 授权")
    auth.add_argument("--oauth-port", type=int, default=DEFAULT_OAUTH_PORT)
    auth.add_argument("--no-open", action="store_true", help="只显示授权链接，不自动打开浏览器")

    subparsers.add_parser("discover", help="发现用户令牌可见的群聊（不包含单聊）")

    sync = subparsers.add_parser("sync", help="同步指定会话最近 N 天的消息")
    sync.add_argument("--chat-id", action="append", required=True, help="可重复指定")
    sync.add_argument("--days", type=int, default=30)
    sync.add_argument("--skip-attachments", action="store_true")
    sync.add_argument(
        "--max-attachment-gib",
        type=float,
        default=DEFAULT_MAX_ATTACHMENT_BYTES / 1024**3,
    )

    reader = subparsers.add_parser("serve", help="启动仅本机可访问的离线阅读器")
    reader.add_argument("--host", default="127.0.0.1")
    reader.add_argument("--port", type=int, default=DEFAULT_READER_PORT)

    subparsers.add_parser("doctor", help="检查 FileVault、磁盘、数据库和授权状态")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = archive_paths(args.archive_dir)
    paths.ensure()
    database = ArchiveDatabase(paths.database)
    database.initialize()
    try:
        if args.command == "init":
            print(f"档案库已初始化：{paths.database}")
        elif args.command == "demo":
            result = seed_demo(database, paths)
            print(
                f"示例数据已就绪：{result['conversations']} 个会话，"
                f"新增 {result['messages_written']} 条消息，{result['attachments']} 个附件"
            )
        elif args.command == "auth":
            _authorize(args.oauth_port, args.no_open)
        elif args.command == "discover":
            client = _client()
            syncer = ArchiveSyncer(
                database,
                client,
                paths,
                max_attachment_bytes=DEFAULT_MAX_ATTACHMENT_BYTES,
            )
            chats = syncer.discover()
            if not chats:
                print("未发现群聊。注意：该官方接口不返回单聊。")
            for item in chats:
                print(f"{item.get('chat_id')}\t{item.get('name') or item.get('chat_name') or '(未命名)'}")
            print(f"共发现 {len(chats)} 个群聊；单聊需要显式提供 chat_id。")
        elif args.command == "sync":
            max_bytes = int(args.max_attachment_gib * 1024**3)
            if max_bytes < 0:
                parser.error("--max-attachment-gib 不能小于 0")
            syncer = ArchiveSyncer(
                database,
                _client(),
                paths,
                max_attachment_bytes=max_bytes,
            )
            counts = syncer.sync(
                args.chat_id,
                days=args.days,
                skip_attachments=args.skip_attachments,
            )
            print(
                f"同步完成：读取 {counts.messages_seen} 条，新增 {counts.messages_written} 条，"
                f"下载附件 {counts.attachments_downloaded} 个，跳过 {counts.attachments_skipped} 个"
            )
        elif args.command == "serve":
            serve(database, paths, args.host, args.port)
        elif args.command == "doctor":
            failed = _doctor(database, paths.root)
            raise SystemExit(1 if failed else 0)
    except (ValueError, FeishuAPIError, KeychainError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _client(oauth_port: int = DEFAULT_OAUTH_PORT) -> FeishuClient:
    return FeishuClient(FeishuAppConfig.from_env(oauth_port), KeychainStore())


def _authorize(oauth_port: int, no_open: bool) -> None:
    config = FeishuAppConfig.from_env(oauth_port)
    parsed_redirect = urllib.parse.urlparse(config.redirect_uri)
    redirect_host = parsed_redirect.hostname or ""
    redirect_port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    if parsed_redirect.path != "/oauth/callback":
        raise ValueError("FEISHU_REDIRECT_URI 路径必须是 /oauth/callback")
    if not is_loopback_host(redirect_host) or redirect_port != oauth_port:
        raise ValueError("OAuth 回调必须指向本机回环地址和 --oauth-port 指定端口")
    client = FeishuClient(config, KeychainStore())
    state = client.new_state()
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/oauth/callback":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            returned_state = (params.get("state") or [""])[0]
            if not secrets.compare_digest(returned_state, state):
                result["error"] = "OAuth state 校验失败"
                self._reply(HTTPStatus.BAD_REQUEST, "授权校验失败，请关闭页面后重试。")
                return
            if params.get("error"):
                result["error"] = (params.get("error") or ["access_denied"])[0]
                self._reply(HTTPStatus.BAD_REQUEST, "你已拒绝授权，可以关闭此页面。")
                return
            code = (params.get("code") or [""])[0]
            if not code:
                result["error"] = "回调中没有 code"
                self._reply(HTTPStatus.BAD_REQUEST, "授权回调缺少 code。")
                return
            result["code"] = code
            self._reply(HTTPStatus.OK, "授权已返回本机，可以关闭此页面。")

        def _reply(self, status: HTTPStatus, text: str) -> None:
            body = f"<!doctype html><meta charset='utf-8'><title>Feishu Archive</title><p>{text}</p>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = HTTPServer((redirect_host, oauth_port), CallbackHandler)
    server.timeout = 1
    url = client.authorization_url(state)
    print("请在 5 分钟内完成飞书授权：")
    print(url)
    if not no_open:
        webbrowser.open(url)
    deadline = time.monotonic() + 300
    try:
        while time.monotonic() < deadline and not result:
            server.handle_request()
    finally:
        server.server_close()
    if result.get("error"):
        raise FeishuAPIError(f"授权失败：{result['error']}")
    if not result.get("code"):
        raise FeishuAPIError("授权等待超时")
    token = client.exchange_code(result["code"])
    print(f"授权成功；令牌已存入 macOS 钥匙串。实际授权范围：{token.scope or '(未返回)'}")


def _doctor(database: ArchiveDatabase, root: Path) -> bool:
    checks: list[tuple[str, bool, str]] = []
    filevault = subprocess.run(
        ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, check=False
    )
    filevault_text = (filevault.stdout or filevault.stderr).strip()
    checks.append(("FileVault", "FileVault is On" in filevault_text, filevault_text))
    usage = shutil.disk_usage(root)
    checks.append(("磁盘剩余", usage.free >= 10 * 1024**3, _format_bytes(usage.free)))
    integrity = database.integrity_check()
    checks.append(("SQLite 完整性", integrity == "ok", integrity))
    checks.append(("阅读器绑定", is_loopback_host("127.0.0.1"), "127.0.0.1 only"))
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    if app_id:
        try:
            token_present = bool(KeychainStore().get(f"{app_id}:refresh_token"))
            checks.append(("OAuth 刷新令牌", token_present, "已保存" if token_present else "未保存"))
        except KeychainError as exc:
            checks.append(("OAuth 刷新令牌", False, str(exc)))
    else:
        checks.append(("飞书应用配置", False, "未设置 FEISHU_APP_ID（demo/阅读不受影响）"))
    for name, ok, detail in checks:
        print(f"{'✓' if ok else '!'} {name}: {detail}")
    hard_failures = [name for name, ok, _ in checks if not ok and name in {"FileVault", "SQLite 完整性", "阅读器绑定"}]
    if hard_failures:
        print("安全检查未通过：" + "、".join(hard_failures))
        return True
    return False


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
