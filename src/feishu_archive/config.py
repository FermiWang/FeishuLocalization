from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ARCHIVE_DIR = Path.home() / "Library" / "Application Support" / "Feishu Archive"
DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024**3
MAX_SINGLE_ATTACHMENT_BYTES = 100 * 1024**2
DEFAULT_READER_PORT = 8765
DEFAULT_OAUTH_PORT = 8766
DEFAULT_INCREMENTAL_DAYS = 2
DEFAULT_SYNC_HOUR = 3
DEFAULT_SYNC_MINUTE = 30
DEFAULT_WIKI_SYNC_HOUR = 3
DEFAULT_WIKI_SYNC_MINUTE = 45
DEFAULT_MAIL_SYNC_HOUR = 4
DEFAULT_MAIL_SYNC_MINUTE = 0
DEFAULT_INSIGHTS_SYNC_HOUR = 4
DEFAULT_INSIGHTS_SYNC_MINUTE = 30
# The backfill LaunchAgent is a KeepAlive daemon; this interval is only its
# launchd ThrottleInterval (minimum seconds between restarts after a crash).
DEFAULT_INSIGHTS_BACKFILL_INTERVAL_SECONDS = 60
DEFAULT_INSIGHTS_BACKFILL_MAX_STEP_SECONDS = 30 * 60
DEFAULT_INSIGHTS_BACKFILL_START_HOUR = 0
DEFAULT_INSIGHTS_BACKFILL_END_HOUR = 24
DEFAULT_INSIGHTS_BACKFILL_MIN_IDLE_SECONDS = 60
# Between two backfill steps where the previous step occupied the engine
# itself, a short settle is enough; the full MIN_IDLE cooldown only applies
# after external engine activity or on process start.
DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS = 10
# Loop pacing: retry delay while the engine is busy, while waiting for new
# work in the monitoring state, and after a step error.
DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS = 30
DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS = 300
DEFAULT_INSIGHTS_BACKFILL_LOOP_ERROR_SECONDS = 60
# Between back-to-back steps the lock is free only for microseconds, so the
# scheduled daily lane (polling every 15s) would starve. Yield the lock for
# longer than that poll interval before immediately continuing.
DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS = 20
# After this many consecutive step errors the loop exits and lets launchd
# apply ThrottleInterval before restarting it.
DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS = 10
DEFAULT_INSIGHTS_TIMEZONE = "Asia/Shanghai"
DEFAULT_VMLX_HOST = "192.168.100.179"
DEFAULT_VMLX_USER = "apple"
DEFAULT_VMLX_MODEL = "vmlx/qwen3.8-27b-8bit"
DEFAULT_VMLX_IDENTITY_FILE: str | None = None
DEFAULT_VMLX_LOCAL_PORT = 18135
# The resident backfill loop uses a separate tunnel port so its frequent SSH
# forwards never collide with a manual or scheduled `insights-run` on 18135.
DEFAULT_INSIGHTS_BACKFILL_LOCAL_PORT = 18136
DEFAULT_VMLX_REMOTE_PORT = 11435
DEFAULT_MEETING_RECORDS_HOST = "192.168.100.179"
DEFAULT_MEETING_RECORDS_USER = "apple"
DEFAULT_MEETING_RECORDS_SYNC_INTERVAL_SECONDS = 300
DEFAULT_MEETING_RECORDS_SYNC_TIMEOUT_SECONDS = 45
DEFAULT_MAIL_INITIAL_DAYS: int | None = None
DEFAULT_MAIL_OVERLAP_DAYS = 2
DEFAULT_MAIL_MAX_PAGES = 5000
DEFAULT_MAX_MAIL_BYTES = 10 * 1024**3
DEFAULT_MAX_MAIL_ATTACHMENT_BYTES = 1024**3
DEFAULT_SCOPES = (
    "im:message:readonly",
    "im:message.p2p_msg:get_as_user",
    "im:message.group_msg:get_as_user",
    "im:chat:readonly",
    "im:chat.members:read",
    "search:message",
    "wiki:wiki:readonly",
    "docx:document:readonly",
    "drive:drive:readonly",
    "offline_access",
)

MAIL_SCOPES = (
    "mail:user_mailbox:readonly",
    "mail:user_mailbox.folder:read",
    "mail:user_mailbox.message:readonly",
    "mail:user_mailbox.message.subject:read",
    "mail:user_mailbox.message.address:read",
    "mail:user_mailbox.message.body:read",
    "offline_access",
)

# OAuth tokens for the mail lane must never share the legacy/default account
# names used by chat and wiki, even when both lanes reuse the same Feishu app.
MAIL_TOKEN_NAMESPACE = "mail"


@dataclass(frozen=True)
class ArchivePaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "archive.sqlite3"

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def knowledge(self) -> Path:
        return self.root / "knowledge"

    @property
    def knowledge_assets(self) -> Path:
        return self.knowledge / "assets"

    @property
    def knowledge_exports(self) -> Path:
        return self.knowledge / "exports"

    @property
    def mail_database(self) -> Path:
        return self.root / "mail.sqlite3"

    @property
    def insights_database(self) -> Path:
        return self.root / "insights.sqlite3"

    @property
    def meeting_records_database(self) -> Path:
        return self.root / "meeting-records.sqlite3"

    @property
    def insights(self) -> Path:
        return self.root / "insights"

    @property
    def insights_exports(self) -> Path:
        return self.insights / "exports"

    @property
    def insights_backfill_state(self) -> Path:
        return self.insights / "backfill-state.json"

    @property
    def insights_backfill_checkpoints(self) -> Path:
        return self.insights / "backfill-checkpoints"

    @property
    def mail(self) -> Path:
        return self.root / "mail"

    @property
    def mail_blobs(self) -> Path:
        return self.mail / "blobs"

    @property
    def mail_tmp(self) -> Path:
        return self.mail / "tmp"

    @property
    def mail_quarantine(self) -> Path:
        return self.mail / "quarantine"

    @property
    def mail_exports(self) -> Path:
        return self.mail / "exports"

    @property
    def sync_lock(self) -> Path:
        return self.root / "sync.lock"

    @property
    def wiki_sync_lock(self) -> Path:
        return self.root / "wiki-sync.lock"

    @property
    def mail_sync_lock(self) -> Path:
        return self.root / "mail-sync.lock"

    @property
    def insights_lock(self) -> Path:
        return self.root / "insights.lock"

    @property
    def meeting_records_sync_lock(self) -> Path:
        return self.root / "meeting-records-sync.lock"

    @property
    def reader_secret(self) -> Path:
        return self.root / "reader.secret"

    @property
    def mail_reader_permanent_unlock(self) -> Path:
        return self.root / "mail-reader.always-unlocked"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachments.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.knowledge_assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.knowledge_exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mail_blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mail_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mail_quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mail_exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.insights_exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.insights_backfill_checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for directory in (
            self.mail,
            self.mail_blobs,
            self.mail_tmp,
            self.mail_quarantine,
            self.mail_exports,
            self.insights,
            self.insights_exports,
            self.insights_backfill_checkpoints,
        ):
            os.chmod(directory, 0o700)


@dataclass(frozen=True)
class FeishuAppConfig:
    app_id: str
    app_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    @classmethod
    def from_env(cls, oauth_port: int = DEFAULT_OAUTH_PORT) -> "FeishuAppConfig":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise ValueError("请先设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量")
        redirect_uri = os.environ.get(
            "FEISHU_REDIRECT_URI",
            f"http://127.0.0.1:{oauth_port}/oauth/callback",
        ).strip()
        return cls(app_id=app_id, app_secret=app_secret, redirect_uri=redirect_uri)


def archive_paths(value: str | Path | None = None) -> ArchivePaths:
    if value is None:
        env_value = os.environ.get("FEISHU_ARCHIVE_DIR")
        value = env_value if env_value else DEFAULT_ARCHIVE_DIR
    return ArchivePaths(Path(value).expanduser().resolve())


def resolve_archive_resource_path(
    root: Path,
    stored_value: str | Path,
    *,
    legacy_anchor: tuple[str, ...] | None = None,
) -> Path:
    """Resolve relative paths and remap legacy absolute resource paths after migration."""
    archive_root = root.expanduser().resolve()
    stored = Path(stored_value)
    candidate = (stored if stored.is_absolute() else archive_root / stored).resolve()
    if candidate == archive_root or archive_root in candidate.parents:
        return candidate
    if legacy_anchor:
        parts = stored.parts
        anchor_size = len(legacy_anchor)
        for index in range(0, len(parts) - anchor_size + 1):
            if tuple(parts[index : index + anchor_size]) != legacy_anchor:
                continue
            remapped = archive_root.joinpath(*parts[index:]).resolve()
            if archive_root in remapped.parents:
                return remapped
    return candidate
