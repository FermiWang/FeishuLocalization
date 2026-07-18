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
from .automation import (
    BackgroundSyncController,
    BackgroundWikiSyncController,
    SyncBusyError,
    run_sync_cycle,
    run_wiki_sync_cycle,
)
from .config import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_OAUTH_PORT,
    DEFAULT_READER_PORT,
    DEFAULT_SYNC_HOUR,
    DEFAULT_SYNC_MINUTE,
    DEFAULT_WIKI_SYNC_HOUR,
    DEFAULT_WIKI_SYNC_MINUTE,
    DEFAULT_SCOPES,
    FeishuAppConfig,
    archive_paths,
)
from .database import ArchiveDatabase
from .demo import seed_demo
from .feishu import FeishuAPIError, FeishuClient
from .keychain import KeychainError, KeychainStore
from .sync import ArchiveSyncer
from .web import is_loopback_host, serve
from .wiki import WikiSyncer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-archive",
        description="通过飞书官方接口建立本机离线消息与知识库档案",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--archive-dir",
        help="档案目录，默认 ~/Library/Application Support/Feishu Archive",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化本地档案库")
    subparsers.add_parser("demo", help="写入三个 PoC 示例会话")

    configure = subparsers.add_parser("configure", help="把飞书应用凭据安全保存到 macOS 钥匙串")
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument(
        "--app-id-stdin",
        action="store_true",
        help="从标准输入读取 App ID",
    )
    configure_mode.add_argument(
        "--app-secret-stdin",
        action="store_true",
        help="从标准输入读取 App Secret（需先保存 App ID）",
    )

    auth = subparsers.add_parser("auth", help="通过浏览器完成飞书用户 OAuth 授权")
    auth.add_argument("--oauth-port", type=int, default=DEFAULT_OAUTH_PORT)
    auth.add_argument("--no-open", action="store_true", help="只显示授权链接，不自动打开浏览器")

    subparsers.add_parser("discover", help="发现用户令牌可见的群聊和有可见消息的单聊")

    sync = subparsers.add_parser("sync", help="同步指定或全部已发现会话的历史消息")
    sync_target = sync.add_mutually_exclusive_group(required=True)
    sync_target.add_argument("--chat-id", action="append", help="可重复指定")
    sync_target.add_argument(
        "--all-discovered",
        action="store_true",
        help="同步本地档案库中全部已发现会话",
    )
    sync.add_argument("--days", type=int, default=None, help="只同步最近 N 天；默认同步全部可获取历史")
    sync.add_argument("--skip-attachments", action="store_true")
    sync.add_argument(
        "--max-attachment-gib",
        type=float,
        default=DEFAULT_MAX_ATTACHMENT_BYTES / 1024**3,
    )

    attachments = subparsers.add_parser(
        "attachments",
        help="并发续传全部已发现会话的待处理图片和收到的文件",
    )
    attachments.add_argument("--workers", type=int, default=4, help="并发数，范围 1 到 8")
    attachments.add_argument(
        "--max-attachment-gib",
        type=float,
        default=DEFAULT_MAX_ATTACHMENT_BYTES / 1024**3,
    )

    scheduled_sync = subparsers.add_parser(
        "scheduled-sync",
        help="发现新会话并执行每日增量同步",
    )
    scheduled_sync.add_argument(
        "--days",
        type=int,
        default=DEFAULT_INCREMENTAL_DAYS,
        help="已有会话的重叠回看天数",
    )

    subparsers.add_parser("wiki-discover", help="发现用户令牌可见的知识空间")

    wiki_sync = subparsers.add_parser("wiki-sync", help="同步知识空间目录、文档和附件")
    wiki_sync.add_argument("--space-id", action="append", help="可重复指定；默认同步全部可见空间")
    wiki_sync.add_argument("--force", action="store_true", help="忽略修改时间并重新同步正文")
    wiki_sync.add_argument(
        "--max-asset-gib",
        type=float,
        default=DEFAULT_MAX_ATTACHMENT_BYTES / 1024**3,
        help="知识库附件总容量上限",
    )

    wiki_scheduled_sync = subparsers.add_parser(
        "wiki-scheduled-sync",
        help="执行知识库每日增量同步",
    )
    wiki_scheduled_sync.add_argument(
        "--max-asset-gib",
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
        elif args.command == "configure":
            _configure(args.app_id_stdin, args.app_secret_stdin)
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
                print("未发现可见会话。")
            for item in chats:
                print(f"{item.get('chat_id')}\t{item.get('name') or item.get('chat_name') or '(未命名)'}")
            p2p_count = sum(1 for item in chats if item.get("chat_mode") == "p2p")
            print(f"共发现 {len(chats)} 个会话，其中单聊 {p2p_count} 个。")
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
            chat_ids = database.conversation_ids() if args.all_discovered else (args.chat_id or [])
            if not chat_ids:
                raise ValueError("没有可同步的已发现会话，请先执行 discover")
            counts = syncer.sync(
                chat_ids,
                days=args.days,
                skip_attachments=args.skip_attachments,
            )
            print(
                f"同步完成：读取 {counts.messages_seen} 条，新增 {counts.messages_written} 条，"
                f"下载资源 {counts.attachments_downloaded} 个，跳过 {counts.attachments_skipped} 个"
            )
            if counts.attachments_pruned:
                print(
                    f"已清理本人上传附件 {counts.attachments_pruned} 个，"
                    f"释放 {counts.attachment_bytes_pruned / 1024**2:.1f} MiB"
                )
        elif args.command == "attachments":
            max_bytes = int(args.max_attachment_gib * 1024**3)
            if max_bytes < 0:
                parser.error("--max-attachment-gib 不能小于 0")
            if not 1 <= args.workers <= 8:
                parser.error("--workers 必须在 1 到 8 之间")
            chat_ids = database.conversation_ids()
            if not chat_ids:
                raise ValueError("没有已发现会话，请先执行 discover")
            syncer = ArchiveSyncer(
                database,
                _client(),
                paths,
                max_attachment_bytes=max_bytes,
            )
            counts = syncer.download_pending_attachments(chat_ids, workers=args.workers)
            print(
                f"资源续传完成：下载 {counts.attachments_downloaded} 个，"
                f"跳过 {counts.attachments_skipped} 个"
            )
        elif args.command == "scheduled-sync":
            try:
                result = run_sync_cycle(
                    database,
                    paths,
                    _client,
                    trigger="scheduled",
                    overlap_days=args.days,
                )
            except SyncBusyError:
                print("已有同步任务正在运行，本次计划任务无需重复启动。")
            else:
                print(
                    f"计划同步完成：发现 {result['conversations_discovered']} 个会话，"
                    f"新增会话 {result['new_conversations']} 个，"
                    f"读取 {result['messages_seen']} 条消息，状态 {result['status']}"
                )
        elif args.command == "wiki-discover":
            spaces = WikiSyncer(database, _client(), paths).discover_spaces()
            if not spaces:
                print("未发现可见知识空间。")
            for item in spaces:
                print(f"{item.get('space_id')}\t{item.get('name') or '(未命名)'}")
            print(f"共发现 {len(spaces)} 个知识空间。")
        elif args.command in {"wiki-sync", "wiki-scheduled-sync"}:
            max_bytes = int(args.max_asset_gib * 1024**3)
            if max_bytes < 0:
                parser.error("--max-asset-gib 不能小于 0")
            try:
                result = run_wiki_sync_cycle(
                    database,
                    paths,
                    _client,
                    trigger="scheduled" if args.command == "wiki-scheduled-sync" else "manual",
                    space_ids=args.space_id if args.command == "wiki-sync" else None,
                    force=args.force if args.command == "wiki-sync" else False,
                    max_asset_bytes=max_bytes,
                )
            except SyncBusyError:
                print("已有知识库同步任务正在运行，本次无需重复启动。")
            else:
                print(
                    f"知识库同步完成：空间 {result['spaces_seen']} 个，"
                    f"节点 {result['nodes_seen']} 个，"
                    f"更新正文 {result['documents_written']} 篇，"
                    f"下载附件 {result['assets_downloaded']} 个，状态 {result['status']}"
                )
        elif args.command == "serve":
            controller = BackgroundSyncController(database, paths, _client)
            wiki_controller = BackgroundWikiSyncController(database, paths, _client)
            serve(
                database,
                paths,
                args.host,
                args.port,
                sync_start=controller.start,
                wiki_sync_start=wiki_controller.start,
                sync_schedule={
                    "enabled": True,
                    "hour": DEFAULT_SYNC_HOUR,
                    "minute": DEFAULT_SYNC_MINUTE,
                    "overlap_days": DEFAULT_INCREMENTAL_DAYS,
                    "description": (
                        f"每天 {DEFAULT_SYNC_HOUR:02d}:{DEFAULT_SYNC_MINUTE:02d} 自动同步"
                    ),
                },
                wiki_sync_schedule={
                    "enabled": True,
                    "hour": DEFAULT_WIKI_SYNC_HOUR,
                    "minute": DEFAULT_WIKI_SYNC_MINUTE,
                    "description": (
                        f"每天 {DEFAULT_WIKI_SYNC_HOUR:02d}:"
                        f"{DEFAULT_WIKI_SYNC_MINUTE:02d} 自动同步知识库"
                    ),
                },
            )
        elif args.command == "doctor":
            failed = _doctor(database, paths.root)
            raise SystemExit(1 if failed else 0)
    except (ValueError, FeishuAPIError, KeychainError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _app_config(
    oauth_port: int = DEFAULT_OAUTH_PORT,
    store: KeychainStore | None = None,
) -> FeishuAppConfig:
    store = store or KeychainStore()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip() or (store.get("app_id") or "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if app_id and not app_secret:
        app_secret = (store.get(f"{app_id}:app_secret") or "").strip()
    if not app_id or not app_secret:
        raise ValueError(
            "请先通过 configure 将 App ID 和 App Secret 保存到钥匙串，"
            "或设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
        )
    redirect_uri = os.environ.get(
        "FEISHU_REDIRECT_URI",
        f"http://127.0.0.1:{oauth_port}/oauth/callback",
    ).strip()
    return FeishuAppConfig(app_id=app_id, app_secret=app_secret, redirect_uri=redirect_uri)


def _configure(app_id_stdin: bool, app_secret_stdin: bool) -> None:
    value = sys.stdin.read().strip()
    if not value:
        raise ValueError("标准输入为空")
    store = KeychainStore()
    if app_id_stdin:
        store.set("app_id", value)
        print("App ID 已保存到 macOS 钥匙串。")
        return
    if app_secret_stdin:
        app_id = (store.get("app_id") or "").strip()
        if not app_id:
            raise ValueError("请先执行 configure --app-id-stdin")
        store.set(f"{app_id}:app_secret", value)
        print("App Secret 已保存到 macOS 钥匙串。")
        return
    raise ValueError("请选择要保存的凭据类型")


def _client(oauth_port: int = DEFAULT_OAUTH_PORT) -> FeishuClient:
    store = KeychainStore()
    return FeishuClient(_app_config(oauth_port, store), store)


def _authorize(oauth_port: int, no_open: bool) -> None:
    store = KeychainStore()
    config = _app_config(oauth_port, store)
    parsed_redirect = urllib.parse.urlparse(config.redirect_uri)
    redirect_host = parsed_redirect.hostname or ""
    redirect_port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    if parsed_redirect.path != "/oauth/callback":
        raise ValueError("FEISHU_REDIRECT_URI 路径必须是 /oauth/callback")
    if not is_loopback_host(redirect_host) or redirect_port != oauth_port:
        raise ValueError("OAuth 回调必须指向本机回环地址和 --oauth-port 指定端口")
    client = FeishuClient(config, store)
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
    store = KeychainStore()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip() or (store.get("app_id") or "").strip()
    if app_id:
        try:
            secret_present = bool(
                os.environ.get("FEISHU_APP_SECRET", "").strip()
                or store.get(f"{app_id}:app_secret")
            )
            checks.append(("飞书应用配置", secret_present, "已保存" if secret_present else "缺少 App Secret"))
            token_present = bool(store.get(f"{app_id}:refresh_token"))
            checks.append(("OAuth 刷新令牌", token_present, "已保存" if token_present else "未保存"))
            granted = set((store.get(f"{app_id}:scope") or "").split())
            required_knowledge = {
                scope
                for scope in DEFAULT_SCOPES
                if scope.startswith(("wiki:", "docx:", "drive:", "sheets:", "bitable:"))
            }
            missing = sorted(required_knowledge - granted)
            checks.append(
                (
                    "知识库 OAuth 权限",
                    not missing,
                    "已授权" if not missing else "需重新执行 auth：" + " ".join(missing),
                )
            )
        except KeychainError as exc:
            checks.append(("OAuth 刷新令牌", False, str(exc)))
    else:
        checks.append(("飞书应用配置", False, "未配置 App ID（demo/阅读不受影响）"))
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
