from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence


MAIL_SCHEMA_VERSION = 1


_MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE mailboxes (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_mailbox_id TEXT NOT NULL,
    primary_email_address TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_at INTEGER,
    last_synced_at INTEGER,
    error TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(provider, provider_mailbox_id)
);

CREATE INDEX idx_mailboxes_email
    ON mailboxes(primary_email_address COLLATE NOCASE);

CREATE TABLE folders (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    provider_folder_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    folder_type TEXT NOT NULL DEFAULT 'unknown',
    parent_provider_folder_id TEXT,
    unread_count INTEGER,
    total_count INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_at INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(mailbox_id, provider_folder_id)
);

CREATE INDEX idx_folders_mailbox_type
    ON folders(mailbox_id, folder_type, name COLLATE NOCASE);

CREATE TABLE blobs (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    relative_path TEXT NOT NULL,
    media_type TEXT,
    status TEXT NOT NULL DEFAULT 'stored',
    created_at INTEGER NOT NULL,
    verified_at INTEGER
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL,
    thread_id TEXT,
    smtp_message_id TEXT,
    subject TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    sender_address TEXT NOT NULL DEFAULT '',
    participants_text TEXT NOT NULL DEFAULT '',
    send_date INTEGER,
    received_date INTEGER,
    message_state TEXT NOT NULL DEFAULT 'unknown',
    priority TEXT,
    has_attachment INTEGER NOT NULL DEFAULT 0 CHECK(has_attachment IN (0, 1)),
    body_plain_text TEXT NOT NULL DEFAULT '',
    body_html_blob_id INTEGER REFERENCES blobs(id),
    raw_blob_id INTEGER REFERENCES blobs(id),
    security_level_json TEXT NOT NULL DEFAULT '{}',
    source_hash TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    archived_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    source_missing_at INTEGER,
    last_missing_checked_at INTEGER,
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_count >= 0),
    tombstoned_at INTEGER,
    UNIQUE(mailbox_id, provider_message_id)
);

CREATE INDEX idx_messages_mailbox_date
    ON messages(mailbox_id, received_date DESC, send_date DESC, provider_message_id);
CREATE INDEX idx_messages_mailbox_thread
    ON messages(mailbox_id, thread_id);
CREATE INDEX idx_messages_smtp_id
    ON messages(smtp_message_id);
CREATE INDEX idx_messages_tombstone
    ON messages(mailbox_id, tombstoned_at, last_seen_at);

CREATE TABLE message_folders (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    PRIMARY KEY(message_id, folder_id)
);

CREATE INDEX idx_message_folders_folder
    ON message_folders(folder_id, message_id);

CREATE TABLE recipients (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('from', 'to', 'cc', 'bcc', 'reply_to')),
    position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0),
    display_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    normalized_address TEXT NOT NULL DEFAULT '',
    provider_id TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(message_id, role, position)
);

CREATE INDEX idx_recipients_address
    ON recipients(normalized_address, message_id);

CREATE TABLE labels (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    provider_label_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    label_type TEXT NOT NULL DEFAULT 'unknown',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(message_id, provider_label_id)
);

CREATE INDEX idx_labels_lookup
    ON labels(provider_label_id, message_id);

CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    provider_attachment_id TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT,
    declared_byte_size INTEGER,
    byte_size INTEGER,
    is_inline INTEGER NOT NULL DEFAULT 0 CHECK(is_inline IN (0, 1)),
    content_id TEXT,
    disposition TEXT,
    blob_id INTEGER REFERENCES blobs(id),
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    downloaded_at INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(message_id, provider_attachment_id)
);

CREATE INDEX idx_attachments_status
    ON attachments(status, id);
CREATE INDEX idx_attachments_blob
    ON attachments(blob_id);

CREATE TABLE sync_state (
    mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    window_start INTEGER,
    window_end INTEGER,
    page_token TEXT,
    last_message_at INTEGER,
    last_synced_at INTEGER,
    status TEXT NOT NULL DEFAULT 'idle',
    error TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(mailbox_id, scope)
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER REFERENCES mailboxes(id) ON DELETE SET NULL,
    trigger TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    window_start INTEGER,
    window_end INTEGER,
    folders_seen INTEGER NOT NULL DEFAULT 0,
    windows_scanned INTEGER NOT NULL DEFAULT 0,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    message_ids_seen INTEGER NOT NULL DEFAULT 0,
    windows_completed INTEGER NOT NULL DEFAULT 0,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    messages_written INTEGER NOT NULL DEFAULT 0,
    raw_messages_saved INTEGER NOT NULL DEFAULT 0,
    attachments_seen INTEGER NOT NULL DEFAULT 0,
    attachments_downloaded INTEGER NOT NULL DEFAULT 0,
    attachments_skipped INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    events_processed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);

CREATE INDEX idx_sync_runs_mailbox
    ON sync_runs(mailbox_id, id DESC);

CREATE TABLE event_inbox (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    provider_message_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    received_at INTEGER NOT NULL,
    processed_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    UNIQUE(mailbox_id, event_id)
);

CREATE INDEX idx_event_inbox_pending
    ON event_inbox(status, received_at, id);

CREATE VIRTUAL TABLE message_fts USING fts5(
    subject,
    body_plain_text,
    sender_name,
    sender_address,
    participants_text,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(
        rowid, subject, body_plain_text, sender_name, sender_address, participants_text
    ) VALUES (
        new.id, new.subject, new.body_plain_text, new.sender_name,
        new.sender_address, new.participants_text
    );
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO message_fts(
        message_fts, rowid, subject, body_plain_text,
        sender_name, sender_address, participants_text
    ) VALUES (
        'delete', old.id, old.subject, old.body_plain_text,
        old.sender_name, old.sender_address, old.participants_text
    );
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO message_fts(
        message_fts, rowid, subject, body_plain_text,
        sender_name, sender_address, participants_text
    ) VALUES (
        'delete', old.id, old.subject, old.body_plain_text,
        old.sender_name, old.sender_address, old.participants_text
    );
    INSERT INTO message_fts(
        rowid, subject, body_plain_text, sender_name, sender_address, participants_text
    ) VALUES (
        new.id, new.subject, new.body_plain_text, new.sender_name,
        new.sender_address, new.participants_text
    );
END;
""",
}


_RECIPIENT_ROLES = ("from", "to", "cc", "bcc", "reply_to")
_INLINE_PAYLOAD_KEYS = {
    "attachment_body",
    "body",
    "body_html",
    "body_plain_text",
    "raw_mime",
}


class MailDatabase:
    """Independent SQLite store for the mail synchronization lane.

    HTML and raw MIME bytes are deliberately absent from the message schema. They
    may only be linked through ``body_html_blob_id`` and ``raw_blob_id``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA secure_delete=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            current = int(con.execute("PRAGMA user_version").fetchone()[0])
            if current > MAIL_SCHEMA_VERSION:
                raise RuntimeError(
                    f"邮件数据库版本 {current} 高于程序支持版本 {MAIL_SCHEMA_VERSION}"
                )
            for version in range(current + 1, MAIL_SCHEMA_VERSION + 1):
                script = _MIGRATIONS.get(version)
                if script is None:
                    raise RuntimeError(f"缺少邮件数据库迁移版本 {version}")
                try:
                    con.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;"
                    )
                except Exception:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise
        os.chmod(self.path, 0o600)

    def schema_version(self) -> int:
        with self.connection() as con:
            return int(con.execute("PRAGMA user_version").fetchone()[0])

    def integrity_check(self) -> str:
        report = self.integrity_report()
        if (
            report["sqlite"] == "ok"
            and report["foreign_key_violations"] == 0
            and report["message_rows"] == report["fts_rows"]
        ):
            return "ok"
        return json.dumps(report, ensure_ascii=False, separators=(",", ":"))

    def integrity_report(self) -> dict[str, Any]:
        with self.connection() as con:
            sqlite_status = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
            message_rows = int(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            fts_rows = int(con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0])
        return {
            "sqlite": sqlite_status,
            "foreign_key_violations": foreign_keys,
            "message_rows": message_rows,
            "fts_rows": fts_rows,
        }

    def blob_integrity_report(self, root: Path | str) -> dict[str, int]:
        """Verify every indexed CAS object without trusting its path or size alone."""

        archive_root = Path(root).resolve()
        checked = 0
        missing = 0
        corrupt = 0
        with self.connection() as con:
            rows = con.execute(
                "SELECT sha256, byte_size, relative_path FROM blobs ORDER BY id"
            ).fetchall()
        for row in rows:
            checked += 1
            try:
                relative_path = str(row["relative_path"])
                _validate_relative_path(relative_path)
                target = (archive_root / relative_path).resolve()
                target.relative_to(archive_root)
            except (ValueError, OSError):
                corrupt += 1
                continue
            if not target.is_file():
                missing += 1
                continue
            expected_size = int(row["byte_size"])
            expected_digest = str(row["sha256"])
            if (
                target.stat().st_size != expected_size
                or _sha256_file(target) != expected_digest
            ):
                corrupt += 1
        return {"checked": checked, "missing": missing, "corrupt": corrupt}

    def upsert_mailbox(self, item: dict[str, Any]) -> int:
        provider = str(item.get("provider") or "feishu").strip().lower()
        provider_mailbox_id = str(
            item.get("provider_mailbox_id") or item.get("mailbox_id") or item.get("id") or ""
        ).strip()
        if not provider_mailbox_id:
            raise ValueError("邮箱缺少 provider_mailbox_id")
        now = _now_ms()
        seen_at = _optional_int(item.get("last_seen_at")) or now
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO mailboxes(
                    provider, provider_mailbox_id, primary_email_address,
                    display_name, status, last_seen_at, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                ON CONFLICT(provider, provider_mailbox_id) DO UPDATE SET
                    primary_email_address=CASE
                        WHEN excluded.primary_email_address=''
                        THEN mailboxes.primary_email_address
                        ELSE excluded.primary_email_address
                    END,
                    display_name=CASE
                        WHEN excluded.display_name='' THEN mailboxes.display_name
                        ELSE excluded.display_name
                    END,
                    status='active',
                    last_seen_at=excluded.last_seen_at,
                    error=NULL,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    provider,
                    provider_mailbox_id,
                    item.get("primary_email_address") or item.get("email_address") or "",
                    item.get("display_name") or item.get("name") or "",
                    seen_at,
                    _metadata_json(item),
                    now,
                    now,
                ),
            )
            row = con.execute(
                "SELECT id FROM mailboxes WHERE provider=? AND provider_mailbox_id=?",
                (provider, provider_mailbox_id),
            ).fetchone()
        return int(row["id"])

    def get_mailbox(self, mailbox_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        return dict(row) if row else None

    def find_mailbox(self, provider: str, provider_mailbox_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM mailboxes WHERE provider=? AND provider_mailbox_id=?",
                (provider.strip().lower(), provider_mailbox_id),
            ).fetchone()
        return dict(row) if row else None

    def list_mailboxes(self) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                "SELECT * FROM mailboxes ORDER BY primary_email_address COLLATE NOCASE, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_folder(
        self,
        mailbox_id: int,
        item: dict[str, Any],
        *,
        seen_at: int | None = None,
    ) -> int:
        with self.connection() as con:
            return self._upsert_folder(con, mailbox_id, item, seen_at or _now_ms())

    def _upsert_folder(
        self,
        con: sqlite3.Connection,
        mailbox_id: int,
        item: dict[str, Any],
        seen_at: int,
    ) -> int:
        provider_folder_id = str(
            item.get("provider_folder_id") or item.get("folder_id") or item.get("id") or ""
        ).strip()
        if not provider_folder_id:
            raise ValueError("邮件文件夹缺少 provider_folder_id")
        existing = con.execute(
            "SELECT * FROM folders WHERE mailbox_id=? AND provider_folder_id=?",
            (mailbox_id, provider_folder_id),
        ).fetchone()
        name = str(item.get("name") or "").strip()
        if not name:
            name = str(existing["name"]) if existing is not None else provider_folder_id
        folder_type = item.get("folder_type") or item.get("type")
        if not folder_type or str(folder_type) == "unknown":
            folder_type = existing["folder_type"] if existing is not None else "unknown"
        if "parent_provider_folder_id" in item or "parent_folder_id" in item:
            parent_provider_folder_id = (
                item.get("parent_provider_folder_id") or item.get("parent_folder_id")
            )
        else:
            parent_provider_folder_id = (
                existing["parent_provider_folder_id"] if existing is not None else None
            )
        unread_count = _optional_int(
            item.get("unread_count")
            if item.get("unread_count") is not None
            else item.get("unread_message_count")
        )
        if unread_count is None and existing is not None:
            unread_count = _optional_int(existing["unread_count"])
        total_count = _optional_int(
            item.get("total_count")
            if item.get("total_count") is not None
            else item.get("message_count")
        )
        if total_count is None and existing is not None:
            total_count = _optional_int(existing["total_count"])
        raw_json = _merged_metadata_json(
            str(existing["raw_json"]) if existing is not None else None,
            item,
        )
        con.execute(
            """
            INSERT INTO folders(
                mailbox_id, provider_folder_id, name, folder_type,
                parent_provider_folder_id, unread_count, total_count,
                status, last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(mailbox_id, provider_folder_id) DO UPDATE SET
                name=excluded.name,
                folder_type=excluded.folder_type,
                parent_provider_folder_id=excluded.parent_provider_folder_id,
                unread_count=excluded.unread_count,
                total_count=excluded.total_count,
                status='active',
                last_seen_at=excluded.last_seen_at,
                raw_json=excluded.raw_json
            """,
            (
                mailbox_id,
                provider_folder_id,
                name,
                folder_type,
                parent_provider_folder_id,
                unread_count,
                total_count,
                seen_at,
                raw_json,
            ),
        )
        row = con.execute(
            "SELECT id FROM folders WHERE mailbox_id=? AND provider_folder_id=?",
            (mailbox_id, provider_folder_id),
        ).fetchone()
        return int(row["id"])

    def replace_folders(
        self,
        mailbox_id: int,
        items: Sequence[dict[str, Any]],
        *,
        seen_at: int | None = None,
    ) -> list[int]:
        seen_at = seen_at or _now_ms()
        with self.transaction() as con:
            ids = [self._upsert_folder(con, mailbox_id, item, seen_at) for item in items]
            con.execute(
                """
                UPDATE folders SET status='missing'
                WHERE mailbox_id=? AND (last_seen_at IS NULL OR last_seen_at<>?)
                """,
                (mailbox_id, seen_at),
            )
        return ids

    def upsert_folders(
        self,
        mailbox_id: int,
        items: Sequence[dict[str, Any]],
        *,
        seen_at: int | None = None,
    ) -> list[int]:
        return self.replace_folders(mailbox_id, items, seen_at=seen_at)

    def list_folders(
        self,
        mailbox_id: int,
        *,
        include_missing: bool = False,
    ) -> list[dict[str, Any]]:
        predicate = (
            "f.mailbox_id=?"
            if include_missing
            else "f.mailbox_id=? AND f.status<>'missing'"
        )
        with self.connection() as con:
            rows = con.execute(
                f"""
                SELECT f.*,
                       (SELECT COUNT(*) FROM message_folders mf
                        JOIN messages m ON m.id=mf.message_id
                        WHERE mf.folder_id=f.id AND m.tombstoned_at IS NULL) AS message_count
                FROM folders f WHERE {predicate}
                ORDER BY folder_type, name COLLATE NOCASE, id
                """,  # noqa: S608
                (mailbox_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_message(
        self,
        mailbox_id: int,
        item: dict[str, Any],
        *,
        recipients: Sequence[dict[str, Any] | str] | None = None,
        recipient_roles: set[str] | None = None,
        labels: Sequence[dict[str, Any] | str] | None = None,
        attachments: Sequence[dict[str, Any]] | None = None,
        seen_at: int | None = None,
    ) -> tuple[int, bool]:
        provider_message_id = str(
            item.get("provider_message_id") or item.get("message_id") or item.get("id") or ""
        ).strip()
        if not provider_message_id:
            raise ValueError("邮件缺少 provider_message_id")
        now = _now_ms()
        seen_at = seen_at or now
        derived_recipients = (
            list(recipients) if recipients is not None else _recipients_from_message(item)
        )
        sender = next(
            (
                _normalize_recipient(value, "from", 0)
                for value in derived_recipients
                if _recipient_role(value) == "from"
            ),
            None,
        )
        with self.transaction() as con:
            existed = con.execute(
                "SELECT * FROM messages WHERE mailbox_id=? AND provider_message_id=?",
                (mailbox_id, provider_message_id),
            ).fetchone()

            def retained(column: str, present: bool, value: Any, default: Any) -> Any:
                if present:
                    return value
                if existed is not None:
                    return existed[column]
                return default

            sender_present = (
                "sender_name" in item
                or "sender_address" in item
                or "head_from" in item
                or "from" in item
                or (
                    recipients is not None
                    and (recipient_roles is None or "from" in recipient_roles)
                )
            )
            sender_name_value = str(
                item.get("sender_name") or (sender or {}).get("display_name") or ""
            )
            sender_address_value = str(
                item.get("sender_address") or (sender or {}).get("address") or ""
            ).lower()
            send_date_present = any(
                key in item for key in ("send_date", "sent_at", "date")
            )
            received_date_present = any(
                key in item for key in ("received_date", "received_at", "internal_date")
            )
            state_present = "message_state" in item or "state" in item
            priority_present = "priority" in item or "priority_type" in item
            attachments_present = attachments is not None or "attachments" in item
            security_present = "security_level_json" in item or "security_level" in item
            source_hash_present = "source_hash" in item or "content_sha256" in item
            source_hash_value = str(
                item.get("source_hash") or item.get("content_sha256") or _source_hash(item)
            )
            security_level_value = _coerce_json_text(
                item.get("security_level_json", item.get("security_level") or {})
            )
            con.execute(
                """
                INSERT INTO messages(
                    mailbox_id, provider_message_id, thread_id, smtp_message_id,
                    subject, sender_name, sender_address, send_date, received_date,
                    message_state, priority, has_attachment, body_plain_text,
                    body_html_blob_id, raw_blob_id, security_level_json,
                    source_hash, raw_json, archived_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mailbox_id, provider_message_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    smtp_message_id=excluded.smtp_message_id,
                    subject=excluded.subject,
                    sender_name=excluded.sender_name,
                    sender_address=excluded.sender_address,
                    send_date=excluded.send_date,
                    received_date=excluded.received_date,
                    message_state=excluded.message_state,
                    priority=excluded.priority,
                    has_attachment=excluded.has_attachment,
                    body_plain_text=excluded.body_plain_text,
                    body_html_blob_id=COALESCE(
                        excluded.body_html_blob_id, messages.body_html_blob_id
                    ),
                    raw_blob_id=COALESCE(excluded.raw_blob_id, messages.raw_blob_id),
                    security_level_json=excluded.security_level_json,
                    source_hash=excluded.source_hash,
                    raw_json=excluded.raw_json,
                    archived_at=excluded.archived_at,
                    last_seen_at=excluded.last_seen_at,
                    source_missing_at=NULL,
                    last_missing_checked_at=NULL,
                    missing_count=0,
                    tombstoned_at=NULL
                """,
                (
                    mailbox_id,
                    provider_message_id,
                    retained("thread_id", "thread_id" in item, item.get("thread_id"), None),
                    retained(
                        "smtp_message_id",
                        "smtp_message_id" in item,
                        item.get("smtp_message_id"),
                        None,
                    ),
                    retained("subject", "subject" in item, item.get("subject") or "", ""),
                    retained("sender_name", sender_present, sender_name_value, ""),
                    retained("sender_address", sender_present, sender_address_value, ""),
                    retained(
                        "send_date",
                        send_date_present,
                        _optional_int(
                            item.get("send_date")
                            if "send_date" in item
                            else item.get("sent_at")
                            if "sent_at" in item
                            else item.get("date")
                        ),
                        None,
                    ),
                    retained(
                        "received_date",
                        received_date_present,
                        _optional_int(
                            item.get("received_date")
                            if "received_date" in item
                            else item.get("received_at")
                            if "received_at" in item
                            else item.get("internal_date")
                        ),
                        None,
                    ),
                    retained(
                        "message_state",
                        state_present,
                        item.get("message_state") or item.get("state") or "unknown",
                        "unknown",
                    ),
                    retained(
                        "priority",
                        priority_present,
                        item.get("priority") or item.get("priority_type"),
                        None,
                    ),
                    retained(
                        "has_attachment",
                        attachments_present or "has_attachment" in item,
                        int(bool(item.get("has_attachment") or attachments or item.get("attachments"))),
                        0,
                    ),
                    retained(
                        "body_plain_text",
                        "body_plain_text" in item,
                        item.get("body_plain_text") or "",
                        "",
                    ),
                    _optional_int(item.get("body_html_blob_id")),
                    _optional_int(item.get("raw_blob_id")),
                    retained(
                        "security_level_json",
                        security_present,
                        security_level_value,
                        "{}",
                    ),
                    retained(
                        "source_hash",
                        source_hash_present,
                        source_hash_value,
                        source_hash_value,
                    ),
                    _merged_metadata_json(
                        str(existed["raw_json"]) if existed is not None else None,
                        item,
                    ),
                    now,
                    seen_at,
                ),
            )
            row = con.execute(
                "SELECT id FROM messages WHERE mailbox_id=? AND provider_message_id=?",
                (mailbox_id, provider_message_id),
            ).fetchone()
            message_id = int(row["id"])
            if recipients is not None or _has_recipient_fields(item):
                if recipient_roles is None:
                    self._replace_recipients(con, message_id, derived_recipients)
                else:
                    self._replace_recipient_roles(
                        con,
                        message_id,
                        derived_recipients,
                        recipient_roles,
                    )
            if labels is not None or "label_ids" in item or "labels" in item:
                label_values = labels if labels is not None else item.get("labels", item.get("label_ids", []))
                self._replace_labels(con, message_id, list(label_values or []))
            if "folder_ids" in item or "folder_id" in item:
                folder_values = item.get("folder_ids")
                if folder_values is None:
                    folder_values = [item.get("folder_id")] if item.get("folder_id") else []
                self._replace_message_folders(con, mailbox_id, message_id, list(folder_values))
            if attachments is not None or "attachments" in item:
                attachment_values = attachments if attachments is not None else item.get("attachments", [])
                self._replace_attachments(con, message_id, list(attachment_values or []))
        return message_id, existed is None

    def replace_recipients(
        self,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
    ) -> None:
        with self.transaction() as con:
            self._replace_recipients(con, message_id, items)

    def _replace_recipients(
        self,
        con: sqlite3.Connection,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
    ) -> None:
        con.execute("DELETE FROM recipients WHERE message_id=?", (message_id,))
        self._insert_recipients(con, message_id, items, set(_RECIPIENT_ROLES))
        self._refresh_message_participants(con, message_id)

    def _replace_recipient_roles(
        self,
        con: sqlite3.Connection,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
        roles: set[str],
    ) -> None:
        selected = {role for role in roles if role in _RECIPIENT_ROLES}
        if not selected:
            return
        placeholders = ",".join("?" for _ in selected)
        con.execute(
            f"DELETE FROM recipients WHERE message_id=? AND role IN ({placeholders})",  # noqa: S608
            (message_id, *sorted(selected)),
        )
        self._insert_recipients(con, message_id, items, selected)
        self._refresh_message_participants(con, message_id)

    def _insert_recipients(
        self,
        con: sqlite3.Connection,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
        roles: set[str],
    ) -> None:
        positions = {role: 0 for role in _RECIPIENT_ROLES}
        for value in items:
            role = _recipient_role(value)
            if role not in roles:
                continue
            recipient = _normalize_recipient(value, role, positions[role])
            positions[role] += 1
            if not recipient["address"] and not recipient["display_name"]:
                continue
            con.execute(
                """
                INSERT INTO recipients(
                    message_id, role, position, display_name, address,
                    normalized_address, provider_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    recipient["role"],
                    recipient["position"],
                    recipient["display_name"],
                    recipient["address"],
                    recipient["normalized_address"],
                    recipient["provider_id"],
                    recipient["raw_json"],
                ),
            )

    def _refresh_message_participants(
        self,
        con: sqlite3.Connection,
        message_id: int,
    ) -> None:
        normalized = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM recipients WHERE message_id=? ORDER BY role, position",
                (message_id,),
            ).fetchall()
        ]
        participant_parts: list[str] = []
        for recipient in normalized:
            participant_parts.extend((recipient["display_name"], recipient["address"]))
        participants_text = " ".join(part for part in participant_parts if part)
        sender = next((item for item in normalized if item["role"] == "from"), None)
        if sender:
            con.execute(
                """
                UPDATE messages
                SET participants_text=?, sender_name=?, sender_address=?
                WHERE id=?
                """,
                (
                    participants_text,
                    sender["display_name"],
                    sender["normalized_address"],
                    message_id,
                ),
            )
        else:
            con.execute(
                """
                UPDATE messages
                SET participants_text=?, sender_name='', sender_address=''
                WHERE id=?
                """,
                (participants_text, message_id),
            )

    def replace_labels(
        self,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
    ) -> None:
        with self.transaction() as con:
            self._replace_labels(con, message_id, items)

    def _replace_labels(
        self,
        con: sqlite3.Connection,
        message_id: int,
        items: Sequence[dict[str, Any] | str],
    ) -> None:
        con.execute("DELETE FROM labels WHERE message_id=?", (message_id,))
        for value in items:
            if isinstance(value, str):
                label_id = value.strip()
                name = value
                label_type = "unknown"
                raw_json = _json_value({"label_id": value})
            else:
                label_id = str(
                    value.get("provider_label_id") or value.get("label_id") or value.get("id") or ""
                ).strip()
                name = value.get("name") or label_id
                label_type = value.get("label_type") or value.get("type") or "unknown"
                raw_json = _metadata_json(value)
            if not label_id:
                continue
            con.execute(
                """
                INSERT INTO labels(
                    message_id, provider_label_id, name, label_type, raw_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, label_id, name, label_type, raw_json),
            )

    def _replace_message_folders(
        self,
        con: sqlite3.Connection,
        mailbox_id: int,
        message_id: int,
        folder_values: Sequence[dict[str, Any] | str],
    ) -> None:
        con.execute("DELETE FROM message_folders WHERE message_id=?", (message_id,))
        for value in folder_values:
            item = value if isinstance(value, dict) else {"folder_id": value, "name": value}
            provider_folder_id = str(
                item.get("provider_folder_id")
                or item.get("folder_id")
                or item.get("id")
                or ""
            ).strip()
            if not provider_folder_id:
                continue
            existing = con.execute(
                "SELECT id FROM folders WHERE mailbox_id=? AND provider_folder_id=?",
                (mailbox_id, provider_folder_id),
            ).fetchone()
            folder_id = (
                int(existing["id"])
                if existing is not None
                else self._upsert_folder(con, mailbox_id, item, _now_ms())
            )
            con.execute(
                "INSERT OR IGNORE INTO message_folders(message_id, folder_id) VALUES (?, ?)",
                (message_id, folder_id),
            )

    def replace_attachments(
        self,
        message_id: int,
        items: Sequence[dict[str, Any]],
    ) -> list[int]:
        with self.transaction() as con:
            return self._replace_attachments(con, message_id, items)

    def _replace_attachments(
        self,
        con: sqlite3.Connection,
        message_id: int,
        items: Sequence[dict[str, Any]],
    ) -> list[int]:
        seen_ids: list[str] = []
        local_ids: list[int] = []
        for item in items:
            attachment_id = self._ensure_attachment(con, message_id, item)
            local_ids.append(attachment_id)
            provider_id = str(
                item.get("provider_attachment_id") or item.get("attachment_id") or item.get("id") or ""
            ).strip()
            if provider_id:
                seen_ids.append(provider_id)
        if seen_ids:
            placeholders = ",".join("?" for _ in seen_ids)
            con.execute(
                f"DELETE FROM attachments WHERE message_id=? "  # noqa: S608
                f"AND provider_attachment_id NOT IN ({placeholders})",
                (message_id, *seen_ids),
            )
        else:
            con.execute("DELETE FROM attachments WHERE message_id=?", (message_id,))
        return local_ids

    def ensure_attachment(self, message_id: int, item: dict[str, Any]) -> int:
        with self.connection() as con:
            return self._ensure_attachment(con, message_id, item)

    def _ensure_attachment(
        self,
        con: sqlite3.Connection,
        message_id: int,
        item: dict[str, Any],
    ) -> int:
        provider_id = str(
            item.get("provider_attachment_id") or item.get("attachment_id") or item.get("id") or ""
        ).strip()
        if not provider_id:
            raise ValueError("邮件附件缺少 provider_attachment_id")
        con.execute(
            """
            INSERT INTO attachments(
                message_id, provider_attachment_id, filename, content_type,
                declared_byte_size, is_inline, content_id, disposition,
                status, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, provider_attachment_id) DO UPDATE SET
                filename=excluded.filename,
                content_type=excluded.content_type,
                declared_byte_size=excluded.declared_byte_size,
                is_inline=excluded.is_inline,
                content_id=excluded.content_id,
                disposition=excluded.disposition,
                raw_json=excluded.raw_json
            """,
            (
                message_id,
                provider_id,
                item.get("filename") or "",
                item.get("content_type") or item.get("mime_type"),
                _optional_int(
                    item.get("declared_byte_size")
                    or item.get("declared_size")
                    or item.get("size")
                ),
                int(bool(item.get("is_inline") or item.get("inline"))),
                item.get("content_id") or item.get("cid"),
                item.get("disposition"),
                item.get("status") or "pending",
                _metadata_json(item),
            ),
        )
        row = con.execute(
            "SELECT id FROM attachments WHERE message_id=? AND provider_attachment_id=?",
            (message_id, provider_id),
        ).fetchone()
        return int(row["id"])

    def update_attachment(self, attachment_id: int, **values: Any) -> None:
        allowed = {
            "filename",
            "content_type",
            "declared_byte_size",
            "byte_size",
            "is_inline",
            "content_id",
            "disposition",
            "blob_id",
            "status",
            "error",
            "downloaded_at",
        }
        filtered = {key: value for key, value in values.items() if key in allowed}
        if not filtered:
            return
        assignments = ", ".join(f"{key}=?" for key in filtered)
        with self.connection() as con:
            con.execute(
                f"UPDATE attachments SET {assignments} WHERE id=?",  # noqa: S608
                (*filtered.values(), attachment_id),
            )

    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                """
                SELECT a.*, b.sha256, b.relative_path, b.media_type AS blob_media_type
                FROM attachments a
                LEFT JOIN blobs b ON b.id=a.blob_id
                WHERE a.id=?
                """,
                (attachment_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_pending_attachments(
        self,
        mailbox_id: int | None = None,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        where = "a.status IN ('pending', 'error')"
        params: list[Any] = []
        if mailbox_id is not None:
            where += " AND m.mailbox_id=?"
            params.append(mailbox_id)
        params.append(limit)
        with self.connection() as con:
            rows = con.execute(
                f"""
                SELECT a.*, m.mailbox_id, m.provider_message_id
                FROM attachments a
                JOIN messages m ON m.id=a.message_id
                WHERE {where}
                ORDER BY a.id
                LIMIT ?
                """,  # noqa: S608
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_blob(
        self,
        sha256: str,
        byte_size: int,
        relative_path: str,
        media_type: str | None = None,
        *,
        status: str = "stored",
        verified_at: int | None = None,
    ) -> int:
        digest = sha256.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("blob sha256 必须是 64 位十六进制字符串")
        byte_size = int(byte_size)
        if byte_size < 0:
            raise ValueError("blob byte_size 不能为负数")
        _validate_relative_path(relative_path)
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO blobs(
                    sha256, byte_size, relative_path, media_type,
                    status, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    byte_size=excluded.byte_size,
                    relative_path=excluded.relative_path,
                    media_type=COALESCE(excluded.media_type, blobs.media_type),
                    status=excluded.status,
                    verified_at=COALESCE(excluded.verified_at, blobs.verified_at)
                """,
                (
                    digest,
                    byte_size,
                    relative_path,
                    media_type,
                    status,
                    _now_ms(),
                    verified_at,
                ),
            )
            row = con.execute("SELECT id FROM blobs WHERE sha256=?", (digest,)).fetchone()
        return int(row["id"])

    def find_blob(self, sha256: str) -> dict[str, Any] | None:
        digest = sha256.strip().lower()
        with self.connection() as con:
            row = con.execute("SELECT * FROM blobs WHERE sha256=?", (digest,)).fetchone()
        return dict(row) if row else None

    def link_attachment_blob(
        self,
        attachment_id: int,
        blob_id: int,
        *,
        downloaded_at: int | None = None,
        sha256: str | None = None,
        byte_size: int | None = None,
        status: str = "downloaded",
        error: str | None = None,
    ) -> None:
        with self.transaction() as con:
            blob = con.execute(
                "SELECT sha256, byte_size FROM blobs WHERE id=?", (blob_id,)
            ).fetchone()
            if blob is None:
                raise ValueError("待关联的邮件 blob 不存在")
            if sha256 is not None and sha256.strip().lower() != str(blob["sha256"]):
                raise ValueError("附件 sha256 与待关联 blob 不一致")
            if byte_size is not None and int(byte_size) != int(blob["byte_size"]):
                raise ValueError("附件 byte_size 与待关联 blob 不一致")
            updated = con.execute(
                """
                UPDATE attachments SET
                    blob_id=?, byte_size=?, status=?, error=?, downloaded_at=?
                WHERE id=?
                """,
                (
                    blob_id,
                    int(blob["byte_size"]),
                    status,
                    error,
                    downloaded_at or _now_ms(),
                    attachment_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("待关联的邮件附件不存在")

    def find_message(
        self,
        mailbox_id: int,
        provider_message_id: str,
    ) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM messages WHERE mailbox_id=? AND provider_message_id=?",
                (mailbox_id, provider_message_id),
            ).fetchone()
        return dict(row) if row else None

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                """
                SELECT m.*, mb.primary_email_address AS mailbox_address
                FROM messages m JOIN mailboxes mb ON mb.id=m.mailbox_id
                WHERE m.id=?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["recipients"] = [
                dict(value)
                for value in con.execute(
                    "SELECT * FROM recipients WHERE message_id=? ORDER BY role, position",
                    (message_id,),
                ).fetchall()
            ]
            result["labels"] = [
                dict(value)
                for value in con.execute(
                    "SELECT * FROM labels WHERE message_id=? ORDER BY name COLLATE NOCASE",
                    (message_id,),
                ).fetchall()
            ]
            result["folders"] = [
                dict(value)
                for value in con.execute(
                    """
                    SELECT f.* FROM folders f
                    JOIN message_folders mf ON mf.folder_id=f.id
                    WHERE mf.message_id=?
                    ORDER BY f.name COLLATE NOCASE
                    """,
                    (message_id,),
                ).fetchall()
            ]
            result["attachments"] = [
                dict(value)
                for value in con.execute(
                    """
                    SELECT a.*, b.sha256, b.relative_path
                    FROM attachments a LEFT JOIN blobs b ON b.id=a.blob_id
                    WHERE a.message_id=? ORDER BY a.id
                    """,
                    (message_id,),
                ).fetchall()
            ]
        return result

    def query_messages(
        self,
        *,
        mailbox_id: int | None = None,
        query: str | None = None,
        folder_id: int | str | None = None,
        label_id: str | None = None,
        date_from_ms: int | None = None,
        date_to_ms: int | None = None,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        joins: list[str] = ["JOIN mailboxes mb ON mb.id=m.mailbox_id"]
        where: list[str] = []
        params: list[Any] = []
        if query and query.strip():
            cleaned_query = query.strip()
            if len(cleaned_query) >= 3:
                joins.append("JOIN message_fts fts ON fts.rowid=m.id")
                where.append("message_fts MATCH ?")
                params.append(_fts_query(cleaned_query))
            else:
                like_value = f"%{_like_query(cleaned_query)}%"
                where.append(
                    "(m.subject LIKE ? ESCAPE '\\' OR m.body_plain_text LIKE ? ESCAPE '\\' "
                    "OR m.sender_name LIKE ? ESCAPE '\\' OR m.sender_address LIKE ? ESCAPE '\\' "
                    "OR m.participants_text LIKE ? ESCAPE '\\')"
                )
                params.extend([like_value] * 5)
        if mailbox_id is not None:
            where.append("m.mailbox_id=?")
            params.append(mailbox_id)
        if folder_id is not None:
            joins.append("JOIN message_folders mf ON mf.message_id=m.id")
            joins.append("JOIN folders fld ON fld.id=mf.folder_id")
            if isinstance(folder_id, int):
                where.append("fld.id=?")
            else:
                where.append("fld.provider_folder_id=?")
            params.append(folder_id)
        if label_id:
            joins.append("JOIN labels lbl ON lbl.message_id=m.id")
            where.append("lbl.provider_label_id=?")
            params.append(label_id)
        if date_from_ms is not None:
            where.append("COALESCE(m.received_date, m.send_date, 0)>=?")
            params.append(date_from_ms)
        if date_to_ms is not None:
            where.append("COALESCE(m.received_date, m.send_date, 0)<?")
            params.append(date_to_ms)
        if not include_tombstoned:
            where.append("m.tombstoned_at IS NULL")
        direction = "DESC" if newest_first else "ASC"
        predicate = " AND ".join(where) if where else "1=1"
        sql = f"""
            SELECT DISTINCT
                   m.id, m.mailbox_id, m.provider_message_id, m.thread_id,
                   m.smtp_message_id, m.subject, m.sender_name, m.sender_address,
                   m.send_date, m.received_date, m.message_state, m.priority,
                   m.has_attachment, m.archived_at, m.last_seen_at,
                   m.tombstoned_at, mb.primary_email_address AS mailbox_address,
                   substr(m.body_plain_text, 1, 500) AS excerpt,
                   (SELECT COUNT(*) FROM attachments a WHERE a.message_id=m.id) AS attachment_count
            FROM messages m
            {' '.join(joins)}
            WHERE {predicate}
            ORDER BY COALESCE(m.received_date, m.send_date, 0) {direction},
                     m.provider_message_id {direction}
            LIMIT ? OFFSET ?
        """
        params.extend((limit, offset))
        try:
            with self.connection() as con:
                rows = con.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                raise ValueError("搜索词包含 FTS5 无法解析的语法") from exc
            raise
        return [dict(row) for row in rows]

    def search_messages(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        return self.query_messages(query=query, **filters)

    def mark_messages_tombstoned(
        self,
        mailbox_id: int,
        provider_message_ids: Sequence[str],
        *,
        tombstoned_at: int | None = None,
    ) -> int:
        message_ids = [str(value) for value in provider_message_ids if str(value)]
        if not message_ids:
            return 0
        timestamp = tombstoned_at or _now_ms()
        placeholders = ",".join("?" for _ in message_ids)
        with self.connection() as con:
            result = con.execute(
                f"""
                UPDATE messages
                SET source_missing_at=COALESCE(source_missing_at, ?),
                    missing_count=MAX(missing_count, 2),
                    tombstoned_at=COALESCE(tombstoned_at, ?)
                WHERE mailbox_id=? AND provider_message_id IN ({placeholders})
                """,  # noqa: S608
                (timestamp, timestamp, mailbox_id, *message_ids),
            )
        return int(result.rowcount)

    def mark_unseen_messages(
        self,
        mailbox_id: int,
        seen_at: int,
        *,
        required_missing_count: int = 2,
    ) -> int:
        required_missing_count = max(1, int(required_missing_count))
        now = _now_ms()
        with self.transaction() as con:
            before = int(
                con.execute(
                    "SELECT COUNT(*) FROM messages WHERE mailbox_id=? AND tombstoned_at IS NOT NULL",
                    (mailbox_id,),
                ).fetchone()[0]
            )
            con.execute(
                """
                UPDATE messages
                SET source_missing_at=COALESCE(source_missing_at, ?),
                    last_missing_checked_at=?,
                    missing_count=missing_count + 1,
                    tombstoned_at=CASE
                        WHEN missing_count + 1 >= ? THEN COALESCE(tombstoned_at, ?)
                        ELSE tombstoned_at
                    END
                WHERE mailbox_id=?
                  AND (last_seen_at IS NULL OR last_seen_at<>?)
                  AND (last_missing_checked_at IS NULL OR last_missing_checked_at<>?)
                """,
                (now, seen_at, required_missing_count, now, mailbox_id, seen_at, seen_at),
            )
            after = int(
                con.execute(
                    "SELECT COUNT(*) FROM messages WHERE mailbox_id=? AND tombstoned_at IS NOT NULL",
                    (mailbox_id,),
                ).fetchone()[0]
            )
        return after - before

    def get_sync_state(self, mailbox_id: int, scope: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM sync_state WHERE mailbox_id=? AND scope=?",
                (mailbox_id, scope),
            ).fetchone()
        return dict(row) if row else None

    def set_sync_state(
        self,
        mailbox_id: int,
        scope: str,
        *,
        window_start: int | None = None,
        window_end: int | None = None,
        page_token: str | None = None,
        last_message_at: int | None = None,
        status: str = "idle",
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = _now_ms()
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO sync_state(
                    mailbox_id, scope, window_start, window_end, page_token,
                    last_message_at, last_synced_at, status, error, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mailbox_id, scope) DO UPDATE SET
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    page_token=excluded.page_token,
                    last_message_at=COALESCE(excluded.last_message_at, sync_state.last_message_at),
                    last_synced_at=excluded.last_synced_at,
                    status=excluded.status,
                    error=excluded.error,
                    extra_json=excluded.extra_json
                """,
                (
                    mailbox_id,
                    scope,
                    window_start,
                    window_end,
                    page_token,
                    last_message_at,
                    now,
                    status,
                    error,
                    _json_value(extra or {}),
                ),
            )

    def start_sync_run(
        self,
        mailbox_id: int,
        trigger: str,
        *,
        window_start: int | None = None,
        window_end: int | None = None,
    ) -> int:
        now = _now_ms()
        with self.transaction() as con:
            con.execute(
                """
                UPDATE sync_runs
                SET finished_at=?, status='error',
                    error=COALESCE(error, '上次邮件同步任务异常中断')
                WHERE mailbox_id=? AND status='running'
                """,
                (now, mailbox_id),
            )
            cursor = con.execute(
                """
                INSERT INTO sync_runs(
                    mailbox_id, trigger, started_at, window_start, window_end
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (mailbox_id, trigger, now, window_start, window_end),
            )
        return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        **counts: int,
    ) -> None:
        allowed = {
            "folders_seen",
            "windows_scanned",
            "pages_scanned",
            "message_ids_seen",
            "windows_completed",
            "messages_seen",
            "messages_written",
            "raw_messages_saved",
            "attachments_seen",
            "attachments_downloaded",
            "attachments_skipped",
            "bytes_downloaded",
            "events_processed",
        }
        values: dict[str, Any] = {
            "finished_at": _now_ms(),
            "status": status,
            "error": error,
        }
        values.update({key: int(value) for key, value in counts.items() if key in allowed})
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connection() as con:
            con.execute(
                f"UPDATE sync_runs SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), run_id),
            )

    def latest_sync_run(self, mailbox_id: int | None = None) -> dict[str, Any] | None:
        with self.connection() as con:
            if mailbox_id is None:
                row = con.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM sync_runs WHERE mailbox_id=? ORDER BY id DESC LIMIT 1",
                    (mailbox_id,),
                ).fetchone()
        return dict(row) if row else None

    def enqueue_event(
        self,
        mailbox_id: int,
        event_id: str,
        event_type: str,
        provider_message_id: str | None,
        payload: dict[str, Any],
        *,
        received_at: int | None = None,
    ) -> tuple[int, bool]:
        event_id = event_id.strip() or hashlib.sha256(
            _json_value(payload).encode("utf-8")
        ).hexdigest()
        with self.transaction() as con:
            existed = con.execute(
                "SELECT id FROM event_inbox WHERE mailbox_id=? AND event_id=?",
                (mailbox_id, event_id),
            ).fetchone()
            con.execute(
                """
                INSERT OR IGNORE INTO event_inbox(
                    mailbox_id, event_id, event_type, provider_message_id,
                    payload_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mailbox_id,
                    event_id,
                    event_type,
                    provider_message_id,
                    _metadata_json(payload),
                    received_at or _now_ms(),
                ),
            )
            row = con.execute(
                "SELECT id FROM event_inbox WHERE mailbox_id=? AND event_id=?",
                (mailbox_id, event_id),
            ).fetchone()
        return int(row["id"]), existed is None

    def pending_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT * FROM event_inbox
                WHERE status IN ('pending', 'error')
                ORDER BY received_at, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_event(
        self,
        event_id: int,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        processed_at = _now_ms() if status == "processed" else None
        with self.connection() as con:
            con.execute(
                """
                UPDATE event_inbox
                SET status=?, error=?, processed_at=?, attempts=attempts+1
                WHERE id=?
                """,
                (status, error, processed_at, event_id),
            )

    def status(self, mailbox_id: int | None = None) -> dict[str, Any]:
        params: tuple[Any, ...] = () if mailbox_id is None else (mailbox_id,)
        message_predicate = "" if mailbox_id is None else " AND m.mailbox_id=?"
        with self.connection() as con:
            if mailbox_id is None:
                mailboxes = int(con.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0])
                folders = int(
                    con.execute(
                        "SELECT COUNT(*) FROM folders WHERE status<>'missing'"
                    ).fetchone()[0]
                )
            else:
                mailboxes = int(
                    con.execute("SELECT COUNT(*) FROM mailboxes WHERE id=?", params).fetchone()[0]
                )
                folders = int(
                    con.execute(
                        "SELECT COUNT(*) FROM folders WHERE mailbox_id=? AND status<>'missing'",
                        params,
                    ).fetchone()[0]
                )
            messages = int(
                con.execute(
                    "SELECT COUNT(*) FROM messages m WHERE m.tombstoned_at IS NULL" + message_predicate,
                    params,
                ).fetchone()[0]
            )
            tombstoned = int(
                con.execute(
                    "SELECT COUNT(*) FROM messages m WHERE m.tombstoned_at IS NOT NULL" + message_predicate,
                    params,
                ).fetchone()[0]
            )
            attachment_rows = con.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN a.status IN ('downloaded', 'available', 'quarantined')
                             THEN 1 ELSE 0 END) AS downloaded,
                    SUM(CASE WHEN a.status='pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN a.status='error' THEN 1 ELSE 0 END) AS failed
                FROM attachments a JOIN messages m ON m.id=a.message_id
                WHERE 1=1
                """ + message_predicate,
                params,
            ).fetchone()
            if mailbox_id is None:
                blob_row = con.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes FROM blobs"
                ).fetchone()
            else:
                blob_row = con.execute(
                    """
                    SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes
                    FROM (
                        SELECT DISTINCT b.id, b.byte_size FROM blobs b
                        WHERE b.id IN (
                            SELECT a.blob_id FROM attachments a
                            JOIN messages m ON m.id=a.message_id
                            WHERE m.mailbox_id=? AND a.blob_id IS NOT NULL
                            UNION
                            SELECT m.body_html_blob_id FROM messages m
                            WHERE m.mailbox_id=? AND m.body_html_blob_id IS NOT NULL
                            UNION
                            SELECT m.raw_blob_id FROM messages m
                            WHERE m.mailbox_id=? AND m.raw_blob_id IS NOT NULL
                        )
                    )
                    """,
                    (mailbox_id, mailbox_id, mailbox_id),
                ).fetchone()
        return {
            "schema_version": self.schema_version(),
            "mailboxes": mailboxes,
            "folders": folders,
            "messages": messages,
            "tombstoned_messages": tombstoned,
            "attachments": int(attachment_rows["total"] or 0),
            "downloaded_attachments": int(attachment_rows["downloaded"] or 0),
            "pending_attachments": int(attachment_rows["pending"] or 0),
            "failed_attachments": int(attachment_rows["failed"] or 0),
            "blobs": int(blob_row["count"] or 0),
            "blob_bytes": int(blob_row["bytes"] or 0),
            "latest_sync": self.latest_sync_run(mailbox_id),
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _scrub_inline_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in _INLINE_PAYLOAD_KEYS:
                continue
            # Mail message ``raw`` is Base64URL MIME. Provider profile/folder
            # ``raw`` metadata is a mapping and remains safe to retain.
            if normalized_key == "raw" and not isinstance(item, (dict, list, tuple)):
                continue
            result[str(key)] = _scrub_inline_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub_inline_payload(item) for item in value]
    return value


def _metadata_json(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("raw_json"), str):
        try:
            value = json.loads(value["raw_json"])
        except json.JSONDecodeError:
            value = {}
    return _json_value(_scrub_inline_payload(value))


def _merged_metadata_json(existing: str | None, value: Any) -> str:
    current: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                current = parsed
        except json.JSONDecodeError:
            pass
    try:
        incoming = json.loads(_metadata_json(value))
    except json.JSONDecodeError:
        incoming = {}
    if isinstance(incoming, dict):
        current.update(incoming)
    return _json_value(current)


def _source_hash(item: dict[str, Any]) -> str:
    normalized = dict(item)
    normalized.pop("source_hash", None)
    normalized.pop("body_html", None)
    normalized.pop("raw_mime", None)
    normalized.pop("raw", None)
    attachments = normalized.get("attachments")
    if isinstance(attachments, list):
        normalized["attachments"] = [
            {key: value for key, value in attachment.items() if key != "body"}
            if isinstance(attachment, dict)
            else attachment
            for attachment in attachments
        ]
    return hashlib.sha256(_json_value(normalized).encode("utf-8")).hexdigest()


def _has_recipient_fields(item: dict[str, Any]) -> bool:
    aliases = ("head_from", "head_to", "to", "head_cc", "cc", "head_bcc", "bcc", "reply_to")
    return "recipients" in item or any(key in item for key in aliases)


def _recipients_from_message(item: dict[str, Any]) -> list[dict[str, Any] | str]:
    if "recipients" in item:
        values = item.get("recipients")
        return list(values) if isinstance(values, (list, tuple)) else []
    result: list[dict[str, Any] | str] = []
    aliases = {
        "from": ("head_from", "from"),
        "to": ("head_to", "to"),
        "cc": ("head_cc", "cc"),
        "bcc": ("head_bcc", "bcc"),
        "reply_to": ("head_reply_to", "reply_to"),
    }
    for role in _RECIPIENT_ROLES:
        value = next((item[key] for key in aliases[role] if key in item), None)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for recipient in values:
            if isinstance(recipient, dict):
                result.append({**recipient, "role": role})
            else:
                result.append({"role": role, "address": str(recipient)})
    return result


def _recipient_role(value: dict[str, Any] | str) -> str:
    if isinstance(value, dict):
        return str(value.get("role") or value.get("kind") or value.get("type") or "to").lower()
    return "to"


def _normalize_recipient(
    value: dict[str, Any] | str,
    role: str,
    position: int,
) -> dict[str, Any]:
    if isinstance(value, str):
        display_name = ""
        address = value.strip()
        provider_id = None
        raw_json = _json_value({"address": address})
    else:
        display_name = str(value.get("display_name") or value.get("name") or "").strip()
        address = str(
            value.get("address")
            or value.get("mail_address")
            or value.get("email_address")
            or value.get("email")
            or ""
        ).strip()
        provider_id = value.get("provider_id") or value.get("id")
        raw_json = _metadata_json(value)
    return {
        "role": role,
        "position": position,
        "display_name": display_name,
        "address": address,
        "normalized_address": address.lower(),
        "provider_id": provider_id,
        "raw_json": raw_json,
    }


def _fts_query(value: str) -> str:
    cleaned = value.strip().replace('"', '""')
    if not cleaned:
        raise ValueError("搜索词不能为空")
    return f'"{cleaned}"'


def _like_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("blob relative_path 必须是安全的相对路径")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return _json_value(value)
        return _json_value(parsed)
    return _json_value(value)
