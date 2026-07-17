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
    def sync_lock(self) -> Path:
        return self.root / "sync.lock"

    @property
    def wiki_sync_lock(self) -> Path:
        return self.root / "wiki-sync.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachments.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.knowledge_assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.knowledge_exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)


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
