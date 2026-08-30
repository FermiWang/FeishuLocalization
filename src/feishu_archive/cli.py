from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .automation import (
    BackgroundMailSyncController,
    BackgroundInsightsRefreshController,
    BackgroundSyncController,
    BackgroundWikiSyncController,
    SyncBusyError,
    acquire_insights_lock,
    acquire_meeting_records_sync_lock,
    run_mail_sync_cycle,
    run_sync_cycle,
    run_wiki_sync_cycle,
)
from .config import (
    DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_END_HOUR,
    DEFAULT_INSIGHTS_BACKFILL_LOCAL_PORT,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_ERROR_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_MIN_IDLE_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_START_HOUR,
    DEFAULT_INSIGHTS_SYNC_HOUR,
    DEFAULT_INSIGHTS_SYNC_MINUTE,
    DEFAULT_INSIGHTS_TIMEZONE,
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_MAIL_INITIAL_DAYS,
    DEFAULT_MAIL_MAX_PAGES,
    DEFAULT_MAIL_OVERLAP_DAYS,
    DEFAULT_MAIL_SYNC_HOUR,
    DEFAULT_MAIL_SYNC_MINUTE,
    DEFAULT_MEETING_RECORDS_HOST,
    DEFAULT_MEETING_RECORDS_SYNC_TIMEOUT_SECONDS,
    DEFAULT_MEETING_RECORDS_USER,
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_BYTES,
    DEFAULT_OAUTH_PORT,
    DEFAULT_READER_PORT,
    DEFAULT_SYNC_HOUR,
    DEFAULT_SYNC_MINUTE,
    DEFAULT_WIKI_SYNC_HOUR,
    DEFAULT_WIKI_SYNC_MINUTE,
    DEFAULT_VMLX_HOST,
    DEFAULT_VMLX_IDENTITY_FILE,
    DEFAULT_VMLX_LOCAL_PORT,
    DEFAULT_VMLX_MODEL,
    DEFAULT_VMLX_REMOTE_PORT,
    DEFAULT_VMLX_USER,
    DEFAULT_SCOPES,
    MAIL_SCOPES,
    MAIL_TOKEN_NAMESPACE,
    ArchivePaths,
    FeishuAppConfig,
    archive_paths,
)
from .database import ArchiveDatabase
from .demo import seed_demo
from .feishu import FeishuAPIError, FeishuClient
from .feishu_mail import FeishuMailProvider
from .keychain import KeychainError, KeychainStore
from .mail_database import MailDatabase
from .meeting_records_database import MeetingRecordsDatabase
from .meeting_records_sync import SSHMeetingRecordsExporter, sync_meeting_records
from .mail_sync import MailAuthorizationError, MailCapacityError, MailSyncPartialError
from .insights import (
    InsightsRunOptions,
    export_report,
    insights_analysis_config,
    insights_run_identity,
    run_daily_insights,
)
from .insights import PROJECTION_VERSION, PROMPT_VERSION
from .insights_database import InsightsDatabase
from .insights_sources import archive_history_bounds, extract_daily_sources
from .backfill import (
    BACKFILL_ANALYSIS_MODE,
    BackfillPolicy,
    backfill_window_remaining_seconds,
    ensure_backfill_state,
    evaluate_vmlx_load,
    load_backfill_state,
    mark_backfill_projection_initialized,
    public_backfill_status,
    record_backfill_audit,
    record_backfill_deferred,
    record_backfill_error,
    record_backfill_success,
    scheduled_backfill_step_budget_seconds,
    within_backfill_window,
)
from .reader_auth import (
    ReaderSessionManager,
    disable_permanent_unlock,
    enable_permanent_unlock,
)
from .sync import ArchiveSyncer
from .vmlx import VMLXResponseError
from .web import is_literal_loopback_host, is_loopback_host, serve
from .wiki import WikiSyncer


VMLX_BEARER_ACCOUNT = "vmlx:api_bearer_token"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-archive",
        description="通过飞书官方接口建立本机离线消息、知识库与邮箱档案",
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

    mail_configure = subparsers.add_parser(
        "mail-configure",
        help="保存独立邮箱应用凭据；未配置时可复用主应用",
    )
    mail_configure_mode = mail_configure.add_mutually_exclusive_group(required=True)
    mail_configure_mode.add_argument("--app-id-stdin", action="store_true", help="从标准输入读取邮箱 App ID")
    mail_configure_mode.add_argument(
        "--app-secret-stdin",
        action="store_true",
        help="从标准输入读取邮箱 App Secret",
    )

    mail_auth = subparsers.add_parser("mail-auth", help="完成飞书邮箱只读 OAuth 授权")
    mail_auth.add_argument("--oauth-port", type=int, default=DEFAULT_OAUTH_PORT)
    mail_auth.add_argument("--no-open", action="store_true", help="只显示授权链接")

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

    wiki_rebuild = subparsers.add_parser(
        "wiki-rebuild",
        help="使用本地原始内容块重建知识库正文，无需访问飞书",
    )
    wiki_rebuild.add_argument("--force", action="store_true", help="即使渲染版本未变化也重新生成")

    mail_sync = subparsers.add_parser("mail-sync", help="同步当前用户飞书邮箱到独立本地邮件库")
    mail_sync.add_argument(
        "--days",
        type=int,
        default=DEFAULT_MAIL_INITIAL_DAYS,
        help="只同步最近 N 天；默认同步全部可获取历史",
    )
    mail_sync.add_argument(
        "--folder",
        action="append",
        metavar="ID_OR_NAME",
        help="可重复指定系统 ID、自定义文件夹 ID、名称或路径；默认同步全部文件夹",
    )
    mail_sync.add_argument("--skip-attachments", action="store_true")
    mail_sync.add_argument(
        "--max-mail-gib",
        type=float,
        default=DEFAULT_MAX_MAIL_BYTES / 1024**3,
    )
    mail_sync.add_argument(
        "--max-attachment-mib",
        type=float,
        default=DEFAULT_MAX_MAIL_ATTACHMENT_BYTES / 1024**2,
    )
    mail_sync.add_argument("--max-pages", type=int, default=DEFAULT_MAIL_MAX_PAGES)

    mail_scheduled_sync = subparsers.add_parser(
        "mail-scheduled-sync",
        help="执行邮箱每日重叠增量同步",
    )
    mail_scheduled_sync.add_argument("--days", type=int, default=DEFAULT_MAIL_OVERLAP_DAYS)
    mail_scheduled_sync.add_argument(
        "--max-mail-gib",
        type=float,
        default=DEFAULT_MAX_MAIL_BYTES / 1024**3,
    )
    mail_scheduled_sync.add_argument(
        "--max-attachment-mib",
        type=float,
        default=DEFAULT_MAX_MAIL_ATTACHMENT_BYTES / 1024**2,
    )
    mail_scheduled_sync.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAIL_MAX_PAGES
    )

    subparsers.add_parser("mail-status", help="显示独立邮箱同步状态")
    subparsers.add_parser("mail-doctor", help="检查邮箱权限、数据库、容量和本机安全边界")
    subparsers.add_parser("mail-preflight", help=argparse.SUPPRESS)
    mail_reader_url = subparsers.add_parser(
        "mail-reader-url",
        help="生成本机邮箱解锁地址，或管理持久本机解锁策略",
    )
    mail_reader_url.add_argument("--host", default="127.0.0.1")
    mail_reader_url.add_argument("--port", type=int, default=DEFAULT_READER_PORT)
    mail_reader_url.add_argument("--open", action="store_true", help="在默认浏览器中打开解锁地址")
    mail_reader_policy = mail_reader_url.add_mutually_exclusive_group()
    mail_reader_policy.add_argument(
        "--permanent",
        action="store_true",
        help="为当前档案永久解除本机邮箱阅读锁定",
    )

    mail_reader_policy.add_argument(
        "--lock",
        action="store_true",
        help="恢复短期会话锁定并撤销现有邮箱会话",
    )

    insights_configure = subparsers.add_parser(
        "insights-configure",
        help="从标准输入把 vMLX Bearer 凭据保存到 macOS 钥匙串",
    )
    insights_configure.add_argument("--bearer-token-stdin", action="store_true", required=True)

    insights_run = subparsers.add_parser(
        "insights-run",
        help="生成指定自然日的本机每日洞察",
    )
    insights_run.add_argument("--date", dest="report_date", help="YYYY-MM-DD；默认昨日")
    insights_run.add_argument("--timezone", default=DEFAULT_INSIGHTS_TIMEZONE)
    insights_run.add_argument("--host", default=DEFAULT_VMLX_HOST)
    insights_run.add_argument("--user", default=DEFAULT_VMLX_USER)
    insights_run.add_argument(
        "--identity-file",
        default=DEFAULT_VMLX_IDENTITY_FILE,
        help="SSH 私钥路径；未指定时只尝试 OpenSSH 默认身份文件",
    )
    insights_run.add_argument("--model", default=DEFAULT_VMLX_MODEL)
    insights_run.add_argument("--local-port", type=int, default=DEFAULT_VMLX_LOCAL_PORT)
    insights_run.add_argument("--remote-port", type=int, choices=(8067, 11435), default=DEFAULT_VMLX_REMOTE_PORT)
    insights_run.add_argument(
        "--no-model",
        action="store_true",
        help="不调用模型，只生成确定性覆盖统计",
    )
    insights_run.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印结果，不写入洞察数据库或导出文件",
    )
    insights_run.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)

    insights_backfill = subparsers.add_parser(
        "insights-backfill-step",
        help="按 vMLX 空闲负荷从最早日期向最近日期自动回填一步",
    )
    insights_backfill.add_argument("--timezone", default=DEFAULT_INSIGHTS_TIMEZONE)
    insights_backfill.add_argument("--host", default=DEFAULT_VMLX_HOST)
    insights_backfill.add_argument("--user", default=DEFAULT_VMLX_USER)
    insights_backfill.add_argument(
        "--identity-file",
        default=DEFAULT_VMLX_IDENTITY_FILE,
        help="SSH 私钥路径；未指定时只尝试 OpenSSH 默认身份文件",
    )
    insights_backfill.add_argument("--model", default=DEFAULT_VMLX_MODEL)
    insights_backfill.add_argument(
        "--local-port", type=int, default=DEFAULT_INSIGHTS_BACKFILL_LOCAL_PORT
    )
    insights_backfill.add_argument(
        "--remote-port", type=int, choices=(11435,), default=DEFAULT_VMLX_REMOTE_PORT
    )
    insights_backfill.add_argument(
        "--minimum-idle-seconds",
        type=int,
        default=DEFAULT_INSIGHTS_BACKFILL_MIN_IDLE_SECONDS,
    )
    insights_backfill.add_argument(
        "--maximum-step-seconds",
        type=int,
        default=DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS,
        help=(
            "单次计划回填的最长运行时间；默认 "
            f"{DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS} 秒"
        ),
    )
    insights_backfill.add_argument(
        "--start-hour", type=int, default=DEFAULT_INSIGHTS_BACKFILL_START_HOUR
    )
    insights_backfill.add_argument(
        "--end-hour", type=int, default=DEFAULT_INSIGHTS_BACKFILL_END_HOUR
    )
    insights_backfill.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    insights_backfill.add_argument(
        "--loop",
        action="store_true",
        help=(
            "常驻循环：只要 vMLX 引擎空闲就连续执行回填步骤，"
            "引擎繁忙或无任务时按内置间隔轮询等待"
        ),
    )

    subparsers.add_parser("insights-status", help="显示每日洞察状态")

    meeting_sync = subparsers.add_parser(
        "meeting-records-sync", help="从 179 增量同步已完成的详细会议记录"
    )
    meeting_sync.add_argument("--host", default=DEFAULT_MEETING_RECORDS_HOST)
    meeting_sync.add_argument("--user", default=DEFAULT_MEETING_RECORDS_USER)
    meeting_sync.add_argument("--identity-file", default=DEFAULT_VMLX_IDENTITY_FILE)
    meeting_sync.add_argument(
        "--timeout", type=int, default=DEFAULT_MEETING_RECORDS_SYNC_TIMEOUT_SECONDS
    )
    meeting_sync.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    subparsers.add_parser("meeting-records-status", help="显示详细会议记录同步和待刷新状态")

    reader = subparsers.add_parser("serve", help="启动仅本机可访问的离线阅读器")
    reader.add_argument("--host", default="127.0.0.1")
    reader.add_argument("--port", type=int, default=DEFAULT_READER_PORT)

    subparsers.add_parser("doctor", help="检查 FileVault、磁盘、数据库和授权状态")
    return parser


def _ensure_archive_paths(paths: ArchivePaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (
        paths.attachments,
        paths.exports,
        paths.knowledge_assets,
        paths.knowledge_exports,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.root, 0o700)


def _ensure_mail_paths(paths: ArchivePaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (
        paths.mail,
        paths.mail_blobs,
        paths.mail_tmp,
        paths.mail_quarantine,
        paths.mail_exports,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    os.chmod(paths.root, 0o700)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = archive_paths(args.archive_dir)
    database: ArchiveDatabase | None = None
    mail_database: MailDatabase | None = None
    meeting_database: MeetingRecordsDatabase | None = None

    archive_commands = {
        "init",
        "demo",
        "discover",
        "sync",
        "attachments",
        "scheduled-sync",
        "wiki-discover",
        "wiki-sync",
        "wiki-scheduled-sync",
        "wiki-rebuild",
        "serve",
        "doctor",
        "insights-backfill-step",
    }
    mail_database_commands = {
        "init",
        "mail-sync",
        "mail-scheduled-sync",
        "mail-status",
        "mail-doctor",
        "mail-preflight",
        "insights-backfill-step",
    }
    meeting_database_commands = {
        "init", "serve", "insights-run", "insights-status", "insights-backfill-step",
        "meeting-records-sync", "meeting-records-status",
    }

    if args.command in archive_commands:
        _ensure_archive_paths(paths)
        database = ArchiveDatabase(paths.database)
        database.initialize()
    if args.command in mail_database_commands:
        _ensure_mail_paths(paths)
        mail_database = MailDatabase(paths.mail_database)
        mail_database.initialize()
    if args.command in meeting_database_commands:
        paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(paths.root, 0o700)
        meeting_database = MeetingRecordsDatabase(paths.meeting_records_database)
        meeting_database.initialize()
    try:
        if args.command == "init":
            assert database is not None
            assert mail_database is not None
            print(f"档案库已初始化：{paths.database}")
            print(f"独立邮件库已初始化：{paths.mail_database}")
            print(f"独立会议记录库已初始化：{paths.meeting_records_database}")
        elif args.command == "demo":
            assert database is not None
            result = seed_demo(database, paths)
            print(
                f"示例数据已就绪：{result['conversations']} 个会话，"
                f"新增 {result['messages_written']} 条消息，{result['attachments']} 个附件"
            )
        elif args.command == "configure":
            _configure(args.app_id_stdin, args.app_secret_stdin)
        elif args.command == "auth":
            _authorize(args.oauth_port, args.no_open)
        elif args.command == "mail-configure":
            _mail_configure(args.app_id_stdin, args.app_secret_stdin)
        elif args.command == "mail-auth":
            _authorize(args.oauth_port, args.no_open, mail=True)
        elif args.command == "discover":
            assert database is not None
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
            assert database is not None
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
            assert database is not None
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
            assert database is not None
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
            assert database is not None
            spaces = WikiSyncer(database, _client(), paths).discover_spaces()
            if not spaces:
                print("未发现可见知识空间。")
            for item in spaces:
                print(f"{item.get('space_id')}\t{item.get('name') or '(未命名)'}")
            print(f"共发现 {len(spaces)} 个知识空间。")
        elif args.command == "wiki-rebuild":
            assert database is not None
            result = WikiSyncer(database, None, paths).rebuild_views(force=args.force)
            print(
                f"知识库正文重建完成：检查 {result['documents_seen']} 篇，"
                f"更新 {result['documents_updated']} 篇，"
                f"跳过 {result['documents_skipped']} 篇，"
                f"渲染版本 {result['render_version']}"
            )
        elif args.command in {"wiki-sync", "wiki-scheduled-sync"}:
            assert database is not None
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
        elif args.command in {"mail-sync", "mail-scheduled-sync"}:
            assert mail_database is not None
            max_mail_bytes = int(args.max_mail_gib * 1024**3)
            max_attachment_bytes = int(args.max_attachment_mib * 1024**2)
            if max_mail_bytes < 0:
                parser.error("--max-mail-gib 不能小于 0")
            if max_attachment_bytes < 0:
                parser.error("--max-attachment-mib 不能小于 0")
            if args.command == "mail-scheduled-sync":
                ready, detail = _mail_oauth_readiness()
                if not ready:
                    print(f"邮箱计划同步已安全跳过：{detail}")
                    return
            try:
                result = run_mail_sync_cycle(
                    mail_database,
                    paths,
                    _mail_provider,
                    trigger="scheduled" if args.command == "mail-scheduled-sync" else "manual",
                    days=args.days,
                    folders=args.folder if args.command == "mail-sync" else None,
                    skip_attachments=(
                        args.skip_attachments if args.command == "mail-sync" else False
                    ),
                    max_mail_bytes=max_mail_bytes,
                    max_attachment_bytes=max_attachment_bytes,
                    max_pages=args.max_pages,
                )
            except SyncBusyError:
                print("已有邮箱同步任务正在运行，本次无需重复启动。")
            else:
                print(
                    f"邮箱同步完成：文件夹 {result['folders_seen']} 个，"
                    f"读取 {result['messages_seen']} 封，"
                    f"新增 {result['messages_written']} 封，"
                    f"下载附件 {result['attachments_downloaded']} 个，"
                    f"状态 {result['status']}"
                )
        elif args.command == "mail-status":
            assert mail_database is not None
            status = mail_database.status()
            mailboxes = mail_database.list_mailboxes()
            status["mailbox"] = mailboxes[0] if mailboxes else None
            status["oauth_ready"], status["oauth_detail"] = _mail_oauth_readiness()
            print(json.dumps(status, ensure_ascii=False, indent=2))
        elif args.command == "mail-preflight":
            assert mail_database is not None
            integrity = mail_database.integrity_check()
            if integrity != "ok":
                raise ValueError(f"邮件数据库预检失败：{integrity}")
            # Exercise MATCH rather than merely checking that the FTS table exists.
            mail_database.query_messages(query="feishu archive preflight", limit=1)
            session = ReaderSessionManager(
                paths.reader_secret,
                permanent_unlock_path=paths.mail_reader_permanent_unlock,
            )
            mode = paths.reader_secret.stat().st_mode & 0o777
            if mode != 0o600 or len(session.unlock_secret) < 32:
                raise ValueError("邮箱解锁密钥预检失败")
            access_mode = "永久本机解锁" if session.permanent_unlock_enabled else "短期本机会话"
            print(f"邮件库 schema/FTS、解锁密钥与访问策略预检通过（{access_mode}）。")
        elif args.command == "mail-reader-url":
            _ensure_mail_paths(paths)
            if not is_literal_loopback_host(args.host) or not is_loopback_host(args.host):
                raise ValueError("邮箱解锁地址只能使用 127.0.0.1 或 localhost")
            if not 1 <= args.port <= 65535:
                raise ValueError("--port 必须在 1 到 65535 之间")
            if args.lock and args.open:
                raise ValueError("--lock 不能与 --open 同时使用")
            if args.lock:
                changed = disable_permanent_unlock(paths.mail_reader_permanent_unlock)
                detail = "已恢复短期会话锁定" if changed else "当前已经是短期会话锁定"
                print(f"{detail}；现有邮箱会话将在下一次请求时撤销。")
                return
            manager = ReaderSessionManager(
                paths.reader_secret,
                permanent_unlock_path=paths.mail_reader_permanent_unlock,
            )
            if args.permanent:
                changed = enable_permanent_unlock(paths.mail_reader_permanent_unlock)
                detail = "已永久解除本机邮箱锁定" if changed else "本机邮箱已处于永久解锁状态"
                print(f"{detail}；服务重启与重新部署后仍会保留。")
            url_host = f"[{args.host}]" if ":" in args.host else args.host
            if manager.permanent_unlock_enabled:
                url = f"http://{url_host}:{args.port}/?mode=mail"
            else:
                fragment = urllib.parse.urlencode({"mail-unlock": manager.unlock_secret})
                url = f"http://{url_host}:{args.port}/?mode=mail#{fragment}"
            print(url)
            if args.open:
                webbrowser.open(url)
        elif args.command == "mail-doctor":
            assert mail_database is not None
            failed = _mail_doctor(mail_database, paths)
            raise SystemExit(1 if failed else 0)
        elif args.command == "meeting-records-status":
            assert meeting_database is not None
            print(
                json.dumps(
                    {**meeting_database.status(), "stale": meeting_database.stale_status()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "meeting-records-sync":
            assert meeting_database is not None
            insights_database = None
            if paths.insights_database.is_file():
                insights_database = InsightsDatabase(paths.insights_database)
                insights_database.initialize()
            lock = acquire_meeting_records_sync_lock(paths)
            try:
                result = sync_meeting_records(
                    meeting_database,
                    insights_database,
                    trigger="scheduled" if args.scheduled else "manual",
                    exporter=SSHMeetingRecordsExporter(
                        host=args.host,
                        user=args.user,
                        identity_file=args.identity_file,
                        timeout=args.timeout,
                    ),
                )
            finally:
                lock.release()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "insights-configure":
            value = sys.stdin.read().strip()
            if not value:
                raise ValueError("标准输入为空")
            KeychainStore().set(VMLX_BEARER_ACCOUNT, value)
            print("vMLX Bearer 凭据已保存到 macOS 钥匙串。")
        elif args.command == "insights-status":
            insights_database = InsightsDatabase(paths.insights_database)
            insights_database.initialize()
            assert meeting_database is not None
            print(
                json.dumps(
                    {
                        **insights_database.status(),
                        "backfill": public_backfill_status(paths.insights_backfill_state),
                        "meeting_records": {
                            **meeting_database.status(),
                            "stale": meeting_database.stale_status(),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "insights-backfill-step":
            assert database is not None
            assert mail_database is not None
            if args.loop:
                _run_insights_backfill_loop(
                    args,
                    paths,
                    database,
                    mail_database,
                    meeting_database=meeting_database,
                )
            else:
                _run_insights_backfill_step(
                    args,
                    paths,
                    database,
                    mail_database,
                    meeting_database=meeting_database,
                )
        elif args.command == "insights-run":
            if not paths.database.is_file():
                raise ValueError(f"聊天与知识库档案不存在：{paths.database}")
            database = ArchiveDatabase(paths.database)
            mail_database = MailDatabase(paths.mail_database) if paths.mail_database.is_file() else None
            report_date = args.report_date or _yesterday(args.timezone)
            insights_database = None if args.dry_run else InsightsDatabase(paths.insights_database)
            if insights_database is not None:
                insights_database.initialize()
            assert meeting_database is not None
            if not args.dry_run:
                meeting_lock = None
                try:
                    meeting_lock = acquire_meeting_records_sync_lock(paths)
                    sync_meeting_records(
                        meeting_database,
                        insights_database,
                        trigger="pre-insights",
                        exporter=SSHMeetingRecordsExporter(
                            host=args.host,
                            user=args.user,
                            identity_file=args.identity_file,
                            timeout=DEFAULT_MEETING_RECORDS_SYNC_TIMEOUT_SECONDS,
                        ),
                    )
                except SyncBusyError:
                    print("会议记录同步正在运行；本次洞察使用当前已完成的本地快照。", file=sys.stderr)
                except Exception as exc:
                    print(f"会议记录同步失败，日报将标记该来源不完整：{type(exc).__name__}: {exc}", file=sys.stderr)
                finally:
                    if meeting_lock is not None:
                        meeting_lock.release()
            if (
                args.scheduled
                and meeting_database.stale_status(report_date).get("status") == "pending"
            ):
                print(
                    json.dumps(
                        {
                            "outcome": "deferred",
                            "reason": "meeting_evidence_refresh_requires_human",
                            "report_date": report_date,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return
            lock = None
            if not args.dry_run:
                lock_deadline = time.monotonic() + (5400 if args.scheduled else 0)
                while lock is None:
                    try:
                        lock = acquire_insights_lock(paths)
                    except SyncBusyError:
                        if not args.scheduled:
                            raise ValueError("另一个洞察任务正在运行，请稍后重试") from None
                        if time.monotonic() >= lock_deadline:
                            print(
                                json.dumps(
                                    {
                                        "outcome": "deferred",
                                        "reason": "insights_lock_busy_timeout",
                                    },
                                    separators=(",", ":"),
                                )
                            )
                            raise SystemExit(75) from None
                        time.sleep(15.0)
            try:
                client = None
                tunnel = None
                model_unavailable_reason = None
                if not args.no_model:
                    from .vmlx import Tunnel, VMLXClient, VMLXError

                    try:
                        bearer = None
                        if args.remote_port == 8067:
                            bearer = (KeychainStore().get(VMLX_BEARER_ACCOUNT) or "").strip()
                            if not bearer:
                                raise ValueError(
                                    "8067 需要 Bearer 凭据；请通过 insights-configure --bearer-token-stdin 保存"
                                )
                        tunnel = Tunnel(
                            host=args.host,
                            user=args.user,
                            local_port=args.local_port,
                            remote_port=args.remote_port,
                            identity_file=args.identity_file,
                        )
                        base_url = tunnel.__enter__()
                        client = VMLXClient(
                            base_url,
                            model=args.model,
                            bearer_token=bearer,
                            timeout=1800.0,
                        )
                        available_models = {
                            str(item.get("id") or "") for item in client.models()
                        }
                        if args.model not in available_models:
                            raise ValueError(f"远端未提供指定模型：{args.model}")
                        if args.remote_port == 11435:
                            # The scheduled daily lane shares the same engine as
                            # historical backfill. Recheck scheduler admission
                            # before every Map/Reduce request so neither lane
                            # blindly submits while vMLX is already occupied.
                            client = _LoadAwareBackfillClient(
                                client,
                                models=[{"id": value} for value in available_models],
                                requested_model=args.model,
                            )
                    except (ValueError, VMLXError) as exc:
                        if tunnel is not None:
                            tunnel.__exit__(*sys.exc_info())
                        tunnel = None
                        client = None
                        model_unavailable_reason = str(exc)
                try:
                    report = run_daily_insights(
                        database,
                        mail_database,
                        insights_database,
                        paths,
                        InsightsRunOptions(
                            report_date=report_date,
                            timezone=args.timezone,
                            model=args.model,
                            dry_run=args.dry_run,
                            trigger="scheduled" if args.scheduled else "manual",
                            model_unavailable_reason=model_unavailable_reason,
                            map_checkpoint_path=(
                                None
                                if args.dry_run
                                else paths.insights_backfill_checkpoints
                                / f"daily-{report_date}.json"
                            ),
                        ),
                        client=client,
                        meeting_database=meeting_database,
                    )
                finally:
                    if tunnel is not None:
                        tunnel.__exit__(None, None, None)
                if not args.dry_run:
                    json_path, markdown_path = export_report(paths, report)
                    report["exports"] = {
                        "json": str(json_path),
                        "markdown": str(markdown_path),
                    }
                if args.scheduled:
                    print(
                        json.dumps(
                            {
                                "outcome": "success" if report.get("published") else "partial",
                                "report_date": report.get("report_date"),
                                "model_status": report.get("model_status"),
                                "published": bool(report.get("published")),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                else:
                    print(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                if lock is not None:
                    lock.release()
        elif args.command == "serve":
            assert database is not None
            assert meeting_database is not None
            controller = BackgroundSyncController(database, paths, _client)
            wiki_controller = BackgroundWikiSyncController(database, paths, _client)
            mail_controller: BackgroundMailSyncController | None = None
            mail_session_manager: ReaderSessionManager | None = None
            mail_unavailable_reason: str | None = None
            insights_database: InsightsDatabase | None = None
            insights_refresh_controller = BackgroundInsightsRefreshController(paths)
            insights_unavailable_reason: str | None = None
            try:
                mail_session_manager = ReaderSessionManager(
                    paths.reader_secret,
                    permanent_unlock_path=paths.mail_reader_permanent_unlock,
                )
            except Exception as exc:
                mail_session_manager = None
                mail_unavailable_reason = f"{type(exc).__name__}: {exc}"
            try:
                insights_database = InsightsDatabase(paths.insights_database)
                insights_database.initialize()
            except Exception as exc:
                insights_database = None
                insights_unavailable_reason = f"{type(exc).__name__}: {exc}"
                print(
                    "警告：每日洞察数据库初始化失败；三条源档案阅读仍可用。",
                    file=sys.stderr,
                )
            try:
                _ensure_mail_paths(paths)
                mail_database = MailDatabase(paths.mail_database)
                mail_database.initialize()
                mail_controller = BackgroundMailSyncController(
                    mail_database,
                    paths,
                    _mail_provider,
                )
            except Exception as exc:
                # Mail is an independent lane. A broken mail database, directory or
                # unlock secret must never take the chat/wiki reader offline.
                mail_database = None
                mail_controller = None
                mail_unavailable_reason = mail_unavailable_reason or f"{type(exc).__name__}: {exc}"
                print(
                    "警告：邮件档案初始化失败，已降级为 503；"
                    "聊天与知识库阅读仍可用。",
                    file=sys.stderr,
                )
            serve(
                database,
                paths,
                args.host,
                args.port,
                sync_start=controller.start,
                wiki_sync_start=wiki_controller.start,
                mail_database=mail_database,
                mail_sync_controller=mail_controller,
                mail_session_manager=mail_session_manager,
                mail_unavailable_reason=mail_unavailable_reason,
                insights_database=insights_database,
                insights_unavailable_reason=insights_unavailable_reason,
                meeting_records_database=meeting_database,
                insights_refresh_controller=insights_refresh_controller,
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
                mail_sync_schedule={
                    "enabled": True,
                    "hour": DEFAULT_MAIL_SYNC_HOUR,
                    "minute": DEFAULT_MAIL_SYNC_MINUTE,
                    "overlap_days": DEFAULT_MAIL_OVERLAP_DAYS,
                    "description": (
                        f"每天 {DEFAULT_MAIL_SYNC_HOUR:02d}:"
                        f"{DEFAULT_MAIL_SYNC_MINUTE:02d} 自动同步邮箱"
                    ),
                },
                insights_schedule={
                    "enabled": True,
                    "hour": DEFAULT_INSIGHTS_SYNC_HOUR,
                    "minute": DEFAULT_INSIGHTS_SYNC_MINUTE,
                    "timezone": DEFAULT_INSIGHTS_TIMEZONE,
                    "description": (
                        f"每天 {DEFAULT_INSIGHTS_SYNC_HOUR:02d}:"
                        f"{DEFAULT_INSIGHTS_SYNC_MINUTE:02d} 生成昨日洞察"
                    ),
                    "backfill": {
                        "enabled": True,
                        "mode": "resident_idle_driven_loop",
                        "poll_seconds": DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS,
                        "monitor_seconds": DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS,
                        "maximum_step_seconds": DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS,
                        "start_hour": DEFAULT_INSIGHTS_BACKFILL_START_HOUR,
                        "end_hour": DEFAULT_INSIGHTS_BACKFILL_END_HOUR,
                        "minimum_idle_seconds": DEFAULT_INSIGHTS_BACKFILL_MIN_IDLE_SECONDS,
                        "continue_minimum_idle_seconds": (
                            DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS
                        ),
                        "direction": "forward",
                    },
                },
            )
        elif args.command == "doctor":
            assert database is not None
            failed = _doctor(database, paths.root)
            raise SystemExit(1 if failed else 0)
    except (
        ValueError,
        FeishuAPIError,
        KeychainError,
        MailAuthorizationError,
        MailCapacityError,
        MailSyncPartialError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _backfill_step_result(state: dict[str, Any], *, outcome: str) -> dict[str, Any]:
    _print_backfill_progress(state, outcome=outcome)
    return {
        "outcome": outcome,
        "reason": state.get("last_reason"),
        "status": state.get("status"),
        "next_date": state.get("next_date"),
        "last_outcome": state.get("last_outcome"),
    }


def _backfill_step_skip_result(outcome: str, reason: str) -> dict[str, Any]:
    print(json.dumps({"outcome": outcome, "reason": reason}, separators=(",", ":")))
    return {
        "outcome": outcome,
        "reason": reason,
        "status": None,
        "next_date": None,
        "last_outcome": None,
    }


def _run_insights_backfill_step(
    args: argparse.Namespace,
    paths: ArchivePaths,
    database: ArchiveDatabase,
    mail_database: MailDatabase,
    *,
    min_idle_seconds: int | None = None,
    meeting_database: MeetingRecordsDatabase | None = None,
) -> dict[str, Any]:
    """Run at most one historical date and emit metadata-only progress.

    ``min_idle_seconds`` overrides the engine idle threshold for this step.
    The resident loop uses it to apply the full cooldown only after external
    engine activity, and a short settle after a step that occupied the engine
    itself.
    """

    idle_threshold = (
        args.minimum_idle_seconds if min_idle_seconds is None else int(min_idle_seconds)
    )
    policy = BackfillPolicy(
        timezone=args.timezone,
        model=args.model,
        start_hour=args.start_hour,
        end_hour=args.end_hour,
        minimum_idle_seconds=idle_threshold,
    )
    insights_database = InsightsDatabase(paths.insights_database)
    insights_database.initialize()
    try:
        lock = acquire_insights_lock(paths)
    except SyncBusyError:
        return _backfill_step_skip_result("deferred", "insights_lock_busy")

    try:
        local_now = datetime.now(ZoneInfo(args.timezone))
        remaining_window_seconds = backfill_window_remaining_seconds(local_now, policy)
        maximum_step_seconds = int(
            getattr(
                args,
                "maximum_step_seconds",
                DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS,
            )
        )
        scheduled_step_seconds = (
            scheduled_backfill_step_budget_seconds(
                local_now,
                policy,
                maximum_step_seconds=maximum_step_seconds,
            )
            if args.scheduled
            else remaining_window_seconds
        )
        if args.scheduled and not within_backfill_window(local_now, policy):
            return _backfill_step_skip_result("deferred", "outside_backfill_window")
        if args.scheduled and scheduled_step_seconds < 900:
            return _backfill_step_skip_result(
                "deferred", "insufficient_backfill_window"
            )
        hard_deadline_monotonic = (
            time.monotonic() + scheduled_step_seconds if args.scheduled else None
        )

        bounds = archive_history_bounds(
            database, mail_database, args.timezone, meeting_database
        )
        oldest = bounds.get("earliest_date")
        if not oldest:
            previous_state = load_backfill_state(paths.insights_backfill_state)
            if previous_state is None:
                return _backfill_step_skip_result("complete", "archive_has_no_evidence")
            # A previously non-empty archive becoming empty is itself a source
            # snapshot change. Keep the prior lower bound so the continuous
            # audit can observe empty days and rebuild stale machine ledgers.
            oldest = previous_state.get("oldest_date")
        yesterday = _yesterday(args.timezone)
        # Always include yesterday in the campaign. Whether it is already
        # covered is an exact snapshot/model/prompt/projection decision made at
        # that date, never an inference from the mere presence of a daily run.
        newest = yesterday
        campaign_options = InsightsRunOptions(
            report_date=newest,
            timezone=args.timezone,
            model=args.model,
            trigger=BACKFILL_ANALYSIS_MODE,
            analysis_mode=BACKFILL_ANALYSIS_MODE,
            activate=False,
            include_carryover=False,
        )
        state = ensure_backfill_state(
            paths.insights_backfill_state,
            oldest_date=str(oldest),
            newest_date=newest,
            timezone=args.timezone,
            model=args.model,
            prompt_version=PROMPT_VERSION,
            analysis_config=insights_analysis_config(campaign_options),
            archive_bounds=bounds,
            extend_newest=True,
        )
        reusable_source: dict[str, Any] | None = None
        audit_date = state.get("audit_next_date")
        if state.get("next_date") is None and audit_date:
            audit_options = InsightsRunOptions(
                report_date=str(audit_date),
                timezone=args.timezone,
                model=args.model,
                trigger=BACKFILL_ANALYSIS_MODE,
                analysis_mode=BACKFILL_ANALYSIS_MODE,
                activate=False,
                include_carryover=False,
            )
            audit_source = extract_daily_sources(
                database, mail_database, str(audit_date), args.timezone, meeting_database
            )
            if not (audit_source.get("coverage") or {}).get("complete"):
                state = record_backfill_deferred(
                    paths.insights_backfill_state,
                    state,
                    reason="source_coverage_incomplete_during_audit",
                )
                return _backfill_step_result(state, outcome="deferred")
            audit_identity = insights_run_identity(audit_source, audit_options)
            state = record_backfill_audit(
                paths.insights_backfill_state,
                state,
                report_date=str(audit_date),
                source_snapshot_hash=str(audit_identity["source_snapshot_hash"]),
            )
            if not state.get("projection_reset_required"):
                return _backfill_step_result(
                    state, outcome=str(state.get("last_outcome") or "audit_match")
                )
            if state.get("next_date") == audit_date:
                reusable_source = audit_source
        if state.get("next_date") is None:
            return _backfill_step_result(state, outcome="complete")
        report_date = str(state["next_date"])
        checkpoint_path = paths.insights_backfill_checkpoints / f"{report_date}.json"
        run_options = InsightsRunOptions(
            report_date=report_date,
            timezone=args.timezone,
            model=args.model,
            trigger=BACKFILL_ANALYSIS_MODE,
            analysis_mode=BACKFILL_ANALYSIS_MODE,
            activate=False,
            include_carryover=False,
            map_checkpoint_path=checkpoint_path,
        )
        source = reusable_source or extract_daily_sources(
            database, mail_database, report_date, args.timezone, meeting_database
        )
        coverage = source.get("coverage") or {}
        if not coverage.get("complete"):
            state = record_backfill_deferred(
                paths.insights_backfill_state,
                state,
                reason="source_coverage_incomplete",
            )
            return _backfill_step_result(state, outcome="deferred")
        if (
            meeting_database is not None
            and meeting_database.stale_status(report_date).get("status") == "pending"
        ):
            state = record_backfill_deferred(
                paths.insights_backfill_state,
                state,
                reason="meeting_evidence_refresh_requires_human",
            )
            return _backfill_step_result(state, outcome="deferred")

        identity = insights_run_identity(source, run_options)
        if state.get("projection_reset_required"):
            include_current = True
            try:
                reset_summary = insights_database.reset_machine_projections(
                    projection_version=PROJECTION_VERSION,
                    include_current=include_current,
                )
                state = mark_backfill_projection_initialized(
                    paths.insights_backfill_state,
                    state,
                    reset_summary=reset_summary,
                )
            except Exception as exc:
                state = record_backfill_error(
                    paths.insights_backfill_state,
                    state,
                    reason=f"projection_reset_failed:{type(exc).__name__}",
                )
                return _backfill_step_result(state, outcome="error")
        daily_coverage = insights_database.matching_successful_report_for_mode(
            report_date=report_date,
            analysis_mode="daily_current",
            timezone=args.timezone,
            model_id=args.model,
            prompt_version=PROMPT_VERSION,
            source_snapshot_hash=str(identity["source_snapshot_hash"]),
            config_requirements={
                "max_chunk_chars": run_options.max_chunk_chars,
                "max_output_tokens": run_options.max_output_tokens,
                "projection_version": PROJECTION_VERSION,
            },
            report_requirements={"published": True},
        )
        daily_report = (daily_coverage or {}).get("report") or {}
        if (
            daily_coverage is not None
            and daily_report.get("model_status") in {"success", "not_required"}
        ):
            try:
                # Repair the narrow crash window where the successful DB run
                # committed before its JSON/Markdown files were exported.
                export_report(paths, dict(daily_report))
                if bool(
                    (state.get("last_projection_reset") or {}).get(
                        "include_current"
                    )
                ):
                    insights_database.replay_run_projections(
                        int(daily_coverage["id"]),
                        campaign_id=str(state["campaign_id"]),
                        projection_version=PROJECTION_VERSION,
                    )
                checkpoint_path.unlink(missing_ok=True)
            except (OSError, ValueError, sqlite3.Error):
                state = record_backfill_error(
                    paths.insights_backfill_state,
                    state,
                    reason="historical_checkpoint_cleanup_failed",
                )
                return _backfill_step_result(state, outcome="error")
            state = record_backfill_success(
                paths.insights_backfill_state,
                state,
                report_date=report_date,
                source_snapshot_hash=str(identity["source_snapshot_hash"]),
                run_id=int(daily_coverage["id"]),
                empty_day=not bool(source.get("evidence")),
                covered_by_daily=True,
            )
            return _backfill_step_result(state, outcome="covered_by_daily")
        existing = insights_database.matching_successful_report(
            report_date=report_date,
            timezone=args.timezone,
            model_id=args.model,
            prompt_version=PROMPT_VERSION,
            source_snapshot_hash=str(identity["source_snapshot_hash"]),
            config=dict(identity["config"]),
        )
        if existing is not None:
            existing_report = dict(existing.get("report") or {})
            try:
                export_report(paths, existing_report)
                if bool(
                    (state.get("last_projection_reset") or {}).get(
                        "include_current"
                    )
                ):
                    insights_database.replay_run_projections(
                        int(existing["id"]),
                        campaign_id=str(state["campaign_id"]),
                        projection_version=PROJECTION_VERSION,
                    )
                checkpoint_path.unlink(missing_ok=True)
            except (OSError, ValueError, sqlite3.Error):
                state = record_backfill_error(
                    paths.insights_backfill_state,
                    state,
                    reason="historical_export_failed",
                )
                return _backfill_step_result(state, outcome="error")
            state = record_backfill_success(
                paths.insights_backfill_state,
                state,
                report_date=report_date,
                source_snapshot_hash=str(identity["source_snapshot_hash"]),
                run_id=int(existing["id"]),
                empty_day=not bool(source.get("evidence")),
                reused=True,
            )
            return _backfill_step_result(state, outcome="reused")

        health_summary: dict[str, Any] = {}
        report: dict[str, Any]
        if not source.get("evidence"):
            # The report engine recognizes an empty, complete day without making
            # a model request.  A non-None sentinel keeps that path publishable.
            try:
                report = run_daily_insights(
                    database,
                    mail_database,
                    insights_database,
                    paths,
                    run_options,
                    client=object(),
                    source=source,
                    meeting_database=meeting_database,
                )
            except Exception as exc:
                state = record_backfill_error(
                    paths.insights_backfill_state,
                    state,
                    reason=f"analysis_failed:{type(exc).__name__}",
                )
                return _backfill_step_result(state, outcome="error")
        else:
            from .vmlx import Tunnel, VMLXClient, VMLXError

            try:
                with Tunnel(
                    host=args.host,
                    user=args.user,
                    local_port=args.local_port,
                    remote_port=args.remote_port,
                    identity_file=args.identity_file,
                ) as base_url:
                    raw_client = VMLXClient(base_url, model=args.model, timeout=1800.0)
                    models = raw_client.models()
                    first = evaluate_vmlx_load(
                        raw_client.health(),
                        models,
                        requested_model=args.model,
                        minimum_idle_seconds=idle_threshold,
                    )
                    first = _warm_cold_start_vmlx(
                        raw_client,
                        first,
                        models=models,
                        requested_model=args.model,
                        minimum_idle_seconds=idle_threshold,
                    )
                    if not first["ready"]:
                        state = record_backfill_deferred(
                            paths.insights_backfill_state,
                            state,
                            reason=str(first["reason"]),
                            health=first["summary"],
                        )
                        return _backfill_step_result(state, outcome="deferred")
                    time.sleep(2.0)
                    second = evaluate_vmlx_load(
                        raw_client.health(),
                        models,
                        requested_model=args.model,
                        minimum_idle_seconds=idle_threshold,
                    )
                    health_summary = dict(second["summary"])
                    health_summary["idle_samples"] = 2
                    if not second["ready"]:
                        state = record_backfill_deferred(
                            paths.insights_backfill_state,
                            state,
                            reason=str(second["reason"]),
                            health=health_summary,
                        )
                        return _backfill_step_result(state, outcome="deferred")
                    client = _LoadAwareBackfillClient(
                        raw_client,
                        models=models,
                        requested_model=args.model,
                        hard_deadline_monotonic=hard_deadline_monotonic,
                    )
                    try:
                        report = run_daily_insights(
                            database,
                            mail_database,
                            insights_database,
                            paths,
                            run_options,
                            client=client,
                            source=source,
                            meeting_database=meeting_database,
                        )
                        if client.step_budget_exhausted:
                            state = record_backfill_deferred(
                                paths.insights_backfill_state,
                                state,
                                reason="scheduled_step_budget_exhausted",
                                health=health_summary,
                            )
                            return _backfill_step_result(state, outcome="deferred")
                    except Exception as exc:
                        state = record_backfill_error(
                            paths.insights_backfill_state,
                            state,
                            reason=f"analysis_failed:{type(exc).__name__}",
                        )
                        return _backfill_step_result(state, outcome="error")
            except (ValueError, VMLXError, OSError, OverflowError):
                state = record_backfill_deferred(
                    paths.insights_backfill_state,
                    state,
                    reason="vmlx_probe_failed",
                )
                return _backfill_step_result(state, outcome="deferred")

        if not report.get("published") or report.get("model_status") not in {
            "success",
            "not_required",
        }:
            state = record_backfill_error(
                paths.insights_backfill_state,
                state,
                reason=f"analysis_not_publishable:{report.get('model_status') or 'unknown'}",
            )
            return _backfill_step_result(state, outcome="error")
        try:
            export_report(paths, report)
        except OSError:
            state = record_backfill_error(
                paths.insights_backfill_state,
                state,
                reason="historical_export_failed",
            )
            return _backfill_step_result(state, outcome="error")
        stored = insights_database.matching_successful_report(
            report_date=report_date,
            timezone=args.timezone,
            model_id=args.model,
            prompt_version=PROMPT_VERSION,
            source_snapshot_hash=str(identity["source_snapshot_hash"]),
            config=dict(identity["config"]),
        )
        state = record_backfill_success(
            paths.insights_backfill_state,
            state,
            report_date=report_date,
            source_snapshot_hash=str(identity["source_snapshot_hash"]),
            run_id=int(stored["id"]) if stored else None,
            empty_day=not bool(source.get("evidence")),
            health=health_summary,
        )
        return _backfill_step_result(state, outcome="success")
    finally:
        lock.release()


def _print_backfill_progress(state: dict[str, Any], *, outcome: str) -> None:
    """Keep LaunchAgent logs free of report or evidence text."""
    print(
        json.dumps(
            {
                "outcome": outcome,
                "status": state.get("status"),
                "last_report_date": state.get("last_report_date"),
                "next_date": state.get("next_date"),
                "audit_next_date": state.get("audit_next_date"),
                "oldest_date": state.get("oldest_date"),
                "newest_date": state.get("newest_date"),
                "processed_days": state.get("processed_days"),
                "audit_cycles_completed": state.get("audit_cycles_completed"),
                "projection_reset_required": state.get("projection_reset_required"),
                "deferred_attempts": state.get("deferred_attempts"),
                "error_attempts": state.get("error_attempts"),
                "reason": state.get("last_reason"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _run_insights_backfill_loop(
    args: argparse.Namespace,
    paths: ArchivePaths,
    database: ArchiveDatabase,
    mail_database: MailDatabase,
    *,
    meeting_database: MeetingRecordsDatabase | None = None,
) -> None:
    """Drive chronological backfill from engine idleness instead of a timer.

    Each iteration runs exactly one locked step, so the daily insights lane can
    still acquire ``insights.lock`` between steps.  A step that occupied the
    engine itself is followed immediately with only a short settle threshold;
    any sign of external activity (busy, cooldown, probe failure, lock held by
    another lane) restores the full minimum-idle requirement and waits before
    the next attempt.  Persistent step errors eventually exit the process so
    launchd applies its ThrottleInterval before restarting the loop.
    """

    # Loop steps reuse the scheduled semantics: the configured window and the
    # per-step time budget still bound every single step.
    args.scheduled = True
    consecutive_errors = 0
    continuation = False
    while True:
        step_kwargs: dict[str, Any] = {
            "min_idle_seconds": (
                DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS
                if continuation
                else None
            )
        }
        if meeting_database is not None:
            step_kwargs["meeting_database"] = meeting_database
        result = _run_insights_backfill_step(
            args,
            paths,
            database,
            mail_database,
            **step_kwargs,
        )
        outcome = str(result.get("outcome") or "")
        reason = str(result.get("reason") or "")
        last_outcome = str(result.get("last_outcome") or "")
        if outcome in {"audit_match", "audit_cycle_complete"}:
            # Audit steps never touch the engine; continue immediately but keep
            # the full idle threshold for the next model-backed step.
            consecutive_errors = 0
            continuation = False
            time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS)
            continue
        if outcome in {"covered_by_daily", "reused"} or (
            outcome == "success" and last_outcome == "empty"
        ):
            # Completed without occupying the engine.
            consecutive_errors = 0
            continuation = False
            time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS)
            continue
        if outcome == "success" or (
            outcome == "deferred" and reason == "scheduled_step_budget_exhausted"
        ):
            # The previous step occupied the engine itself; a short settle is
            # enough before submitting the next date.
            consecutive_errors = 0
            continuation = True
            time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS)
            continue
        if outcome == "complete":
            consecutive_errors = 0
            continuation = False
            time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS)
            continue
        if outcome == "error":
            consecutive_errors += 1
            continuation = False
            if (
                consecutive_errors
                >= DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS
            ):
                print(
                    json.dumps(
                        {
                            "outcome": "abort",
                            "reason": "consecutive_step_errors",
                            "attempts": consecutive_errors,
                        },
                        separators=(",", ":"),
                    )
                )
                raise SystemExit(1)
            time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_ERROR_SECONDS)
            continue
        # deferred: engine busy/cooldown, probe failure, busy lock, incomplete
        # source coverage, or outside the configured window.
        consecutive_errors = 0
        continuation = False
        time.sleep(DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS)


_VMLX_COLD_START_WARMUP_MESSAGES = [
    {
        "role": "system",
        "content": 'Return exactly one JSON object: {"ready":true}',
    },
    {"role": "user", "content": "local engine warmup"},
]


def _attempt_vmlx_cold_start_warmup(client: Any) -> None:
    """Issue a content-free inference so vMLX starts its idle timer."""
    try:
        client.chat_json(
            _VMLX_COLD_START_WARMUP_MESSAGES,
            max_tokens=16,
            temperature=0.0,
        )
    except Exception:
        # vMLX records inference arrival before response parsing. The fresh
        # health probe remains the source of truth if parsing or transport fails.
        pass


def _warm_cold_start_vmlx(
    client: Any,
    decision: dict[str, Any],
    *,
    models: list[dict[str, Any]],
    requested_model: str,
    minimum_idle_seconds: int,
) -> dict[str, Any]:
    """Prime a healthy idle engine whose last-request clock is uninitialized."""
    if decision.get("reason") != "vmlx_last_request_uninitialized":
        return decision
    _attempt_vmlx_cold_start_warmup(client)
    refreshed = evaluate_vmlx_load(
        client.health(),
        models,
        requested_model=requested_model,
        minimum_idle_seconds=minimum_idle_seconds,
    )
    refreshed["summary"] = {
        **dict(refreshed.get("summary") or {}),
        "cold_start_warmup_attempted": True,
    }
    return refreshed


class _LoadAwareBackfillClient:
    """Recheck direct-engine scheduler admission before every model request."""

    def __init__(
        self,
        client: Any,
        *,
        models: list[dict[str, Any]],
        requested_model: str,
        maximum_wait_seconds: float = 900.0,
        poll_seconds: float = 5.0,
        stability_seconds: float = 2.0,
        hard_deadline_monotonic: float | None = None,
        minimum_call_budget_seconds: float = 420.0,
    ) -> None:
        self.client = client
        self.models = models
        self.requested_model = requested_model
        self.maximum_wait_seconds = max(0.0, float(maximum_wait_seconds))
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.stability_seconds = max(0.0, float(stability_seconds))
        self.hard_deadline_monotonic = hard_deadline_monotonic
        self.minimum_call_budget_seconds = max(0.0, float(minimum_call_budget_seconds))
        self.blocked = False
        self.cold_start_warmup_attempted = False
        self.step_budget_exhausted = False

    def _raise_step_budget_exhausted(self) -> None:
        self.blocked = True
        self.step_budget_exhausted = True
        raise RuntimeError("historical_backfill_window_closed")

    def chat_json(self, messages: Any, *, max_tokens: int, temperature: float) -> dict[str, Any]:
        if self.blocked:
            raise RuntimeError("historical_backfill_load_gate_closed")
        now = time.monotonic()
        admission_deadline = (
            self.hard_deadline_monotonic - self.minimum_call_budget_seconds
            if self.hard_deadline_monotonic is not None
            else None
        )
        if admission_deadline is not None and now >= admission_deadline:
            self._raise_step_budget_exhausted()
        deadline = now + self.maximum_wait_seconds
        if admission_deadline is not None:
            deadline = min(deadline, admission_deadline)
        ready_samples = 0
        health_errors = 0
        while True:
            if admission_deadline is not None and time.monotonic() >= admission_deadline:
                self._raise_step_budget_exhausted()
            try:
                load = evaluate_vmlx_load(
                    self.client.health(),
                    self.models,
                    requested_model=self.requested_model,
                    minimum_idle_seconds=0,
                )
                health_errors = 0
            except Exception:
                # A single transient health-probe error (tunnel blip, engine
                # briefly busy right after a long generation) must not latch
                # the gate: the step may already have invested tens of minutes
                # in earlier requests. Tolerate a few probes within the same
                # admission deadline before closing.
                health_errors += 1
                if health_errors < 3 and time.monotonic() < deadline:
                    time.sleep(self.poll_seconds)
                    continue
                self.blocked = True
                raise RuntimeError("historical_backfill_load_gate_closed") from None
            if (
                load.get("reason") == "vmlx_last_request_uninitialized"
                and not self.cold_start_warmup_attempted
            ):
                self.cold_start_warmup_attempted = True
                _attempt_vmlx_cold_start_warmup(self.client)
                ready_samples = 0
                continue
            if load["ready"]:
                ready_samples += 1
                if ready_samples >= 2 or self.stability_seconds == 0:
                    break
                if time.monotonic() + self.stability_seconds >= deadline:
                    self._raise_step_budget_exhausted()
                time.sleep(self.stability_seconds)
                continue
            ready_samples = 0
            if load.get("state") in {"busy", "cooldown"} and time.monotonic() < deadline:
                time.sleep(self.poll_seconds)
                continue
            self.blocked = True
            raise RuntimeError("historical_backfill_load_gate_closed")
        if admission_deadline is not None and time.monotonic() >= admission_deadline:
            self._raise_step_budget_exhausted()
        try:
            return self.client.chat_json(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except VMLXResponseError:
            # The engine answered but its payload was unusable (e.g. truncated
            # JSON at the max_tokens limit). The engine itself is healthy, so
            # the gate stays open and _chat_json_with_token_retry can retry
            # this call with a larger token budget.
            raise
        except Exception:
            self.blocked = True
            raise


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


def _yesterday(timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone}") from exc
    return (datetime.now(zone).date() - timedelta(days=1)).isoformat()


def _mail_app_config(
    oauth_port: int = DEFAULT_OAUTH_PORT,
    store: KeychainStore | None = None,
) -> FeishuAppConfig:
    store = store or KeychainStore()
    mail_app_id = os.environ.get("FEISHU_MAIL_APP_ID", "").strip() or (
        store.get("mail_app_id") or ""
    ).strip()
    if mail_app_id:
        app_secret = os.environ.get("FEISHU_MAIL_APP_SECRET", "").strip() or (
            store.get(f"{mail_app_id}:app_secret") or ""
        ).strip()
        if not app_secret:
            raise ValueError("已配置邮箱 App ID，但缺少对应 App Secret")
        redirect_uri = os.environ.get(
            "FEISHU_MAIL_REDIRECT_URI",
            f"http://127.0.0.1:{oauth_port}/oauth/callback",
        ).strip()
        return FeishuAppConfig(
            app_id=mail_app_id,
            app_secret=app_secret,
            redirect_uri=redirect_uri,
            scopes=MAIL_SCOPES,
        )

    shared = _app_config(oauth_port, store)
    scopes = tuple(dict.fromkeys((*DEFAULT_SCOPES, *MAIL_SCOPES)))
    return FeishuAppConfig(
        app_id=shared.app_id,
        app_secret=shared.app_secret,
        redirect_uri=shared.redirect_uri,
        scopes=scopes,
    )


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


def _mail_configure(app_id_stdin: bool, app_secret_stdin: bool) -> None:
    value = sys.stdin.read().strip()
    if not value:
        raise ValueError("标准输入为空")
    store = KeychainStore()
    if app_id_stdin:
        store.set("mail_app_id", value)
        print("邮箱 App ID 已保存到 macOS 钥匙串。")
        return
    if app_secret_stdin:
        app_id = (store.get("mail_app_id") or "").strip()
        if not app_id:
            raise ValueError("请先执行 mail-configure --app-id-stdin")
        store.set(f"{app_id}:app_secret", value)
        print("邮箱 App Secret 已保存到 macOS 钥匙串。")
        return
    raise ValueError("请选择要保存的邮箱凭据类型")


def _client(
    oauth_port: int = DEFAULT_OAUTH_PORT,
    store: KeychainStore | None = None,
) -> FeishuClient:
    store = store or KeychainStore()
    return FeishuClient(_app_config(oauth_port, store), store)


def _mail_client(
    oauth_port: int = DEFAULT_OAUTH_PORT,
    store: KeychainStore | None = None,
) -> FeishuClient:
    store = store or KeychainStore()
    return FeishuClient(
        _mail_app_config(oauth_port, store),
        store,
        token_namespace=MAIL_TOKEN_NAMESPACE,
    )


def _mail_provider() -> FeishuMailProvider:
    return FeishuMailProvider(_mail_client())


def _mail_oauth_readiness(
    store: KeychainStore | None = None,
) -> tuple[bool, str]:
    store = store or KeychainStore()
    try:
        client = _mail_client(store=store)
    except (ValueError, KeychainError) as exc:
        return False, str(exc)
    try:
        if not store.get(client.account("refresh_token")):
            return False, "未找到邮箱 OAuth 刷新令牌，请执行 mail-auth"
        granted = client.authorized_scopes()
    except KeychainError as exc:
        return False, str(exc)
    required = set(MAIL_SCOPES) - {"offline_access"}
    missing = sorted(required - granted)
    if missing:
        return False, "需重新执行 mail-auth 授权：" + " ".join(missing)
    return True, f"应用 {client.config.app_id} 的只读邮箱授权已就绪"


def _authorize(oauth_port: int, no_open: bool, *, mail: bool = False) -> None:
    store = KeychainStore()
    client = _mail_client(oauth_port, store) if mail else _client(oauth_port, store)
    config = client.config
    parsed_redirect = urllib.parse.urlparse(config.redirect_uri)
    redirect_host = parsed_redirect.hostname or ""
    redirect_port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    if parsed_redirect.path != "/oauth/callback":
        raise ValueError("FEISHU_REDIRECT_URI 路径必须是 /oauth/callback")
    if not is_loopback_host(redirect_host) or redirect_port != oauth_port:
        raise ValueError("OAuth 回调必须指向本机回环地址和 --oauth-port 指定端口")
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
    purpose = "飞书邮箱只读授权" if mail else "飞书授权"
    print(f"请在 5 分钟内完成{purpose}：")
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
            required_main = set(DEFAULT_SCOPES) - {"offline_access"}
            missing = sorted(required_main - granted)
            checks.append(
                (
                    "飞书主 OAuth 权限",
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
    failures = [name for name, ok, _ in checks if not ok]
    if failures:
        print("检查未通过：" + "、".join(failures))
        return True
    return False


def _mail_doctor(database: MailDatabase, paths: ArchivePaths) -> bool:
    checks: list[tuple[str, bool, str]] = []
    filevault = subprocess.run(
        ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, check=False
    )
    filevault_text = (filevault.stdout or filevault.stderr).strip()
    checks.append(("FileVault", "FileVault is On" in filevault_text, filevault_text))

    usage = shutil.disk_usage(paths.root)
    used_ratio = usage.used / usage.total if usage.total else 1.0
    disk_ok = usage.free >= 75 * 1024**3 and used_ratio < 0.97
    checks.append(
        (
            "邮件写入容量",
            disk_ok,
            f"剩余 {_format_bytes(usage.free)}，已用 {used_ratio:.1%}",
        )
    )
    integrity = database.integrity_check()
    checks.append(("邮件 SQLite 完整性", integrity == "ok", integrity))
    blob_integrity = database.blob_integrity_report(paths.root)
    blobs_ok = not blob_integrity["missing"] and not blob_integrity["corrupt"]
    checks.append(
        (
            "邮件 CAS 完整性",
            blobs_ok,
            (
                f"检查 {blob_integrity['checked']} 个，"
                f"缺失 {blob_integrity['missing']} 个，"
                f"损坏 {blob_integrity['corrupt']} 个"
            ),
        )
    )
    checks.append(("阅读器绑定", is_loopback_host("127.0.0.1"), "127.0.0.1 only"))

    session: ReaderSessionManager | None = None
    access_policy_ok = True
    access_policy_detail = ""
    try:
        session = ReaderSessionManager(
            paths.reader_secret,
            permanent_unlock_path=paths.mail_reader_permanent_unlock,
        )
        access_policy_detail = (
            "永久本机解锁（跨重启保留）"
            if session.permanent_unlock_enabled
            else "短期本机会话（15 分钟）"
        )
    except ValueError as exc:
        access_policy_ok = False
        access_policy_detail = str(exc)
    secret_mode = paths.reader_secret.stat().st_mode & 0o777
    checks.append(
        (
            "解锁密钥权限",
            secret_mode == 0o600 and bool(session and len(session.unlock_secret) >= 32),
            oct(secret_mode),
        )
    )
    checks.append(("邮箱访问策略", access_policy_ok, access_policy_detail))
    mail_mode = paths.mail_database.stat().st_mode & 0o777
    checks.append(("邮件库权限", mail_mode == 0o600, oct(mail_mode)))
    directory_modes = {
        str(path.relative_to(paths.root)): oct(path.stat().st_mode & 0o777)
        for path in (paths.mail, paths.mail_blobs, paths.mail_tmp, paths.mail_quarantine)
    }
    directories_ok = all(value == "0o700" for value in directory_modes.values())
    checks.append(
        (
            "邮件目录权限",
            directories_ok,
            ", ".join(f"{name}={mode}" for name, mode in directory_modes.items()),
        )
    )
    oauth_ready, oauth_detail = _mail_oauth_readiness()
    checks.append(("邮箱只读 OAuth", oauth_ready, oauth_detail))

    for name, ok, detail in checks:
        print(f"{'✓' if ok else '!'} {name}: {detail}")
    return any(not ok for _, ok, _ in checks)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
