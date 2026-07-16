from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    chat_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    chat_mode TEXT NOT NULL DEFAULT 'unknown',
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    external INTEGER NOT NULL DEFAULT 0 CHECK (external IN (0, 1)),
    owner_id TEXT,
    avatar_url TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    first_message_at INTEGER,
    last_message_at INTEGER,
    last_synced_at INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS members (
    chat_id TEXT NOT NULL REFERENCES conversations(chat_id) ON DELETE CASCADE,
    member_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    member_type TEXT NOT NULL DEFAULT 'user',
    tenant_key TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chat_id, member_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL REFERENCES conversations(chat_id) ON DELETE CASCADE,
    thread_id TEXT,
    parent_id TEXT,
    root_id TEXT,
    message_type TEXT NOT NULL,
    sender_id TEXT,
    sender_type TEXT,
    sender_name TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    recalled INTEGER NOT NULL DEFAULT 0 CHECK (recalled IN (0, 1)),
    body_text TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    archived_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_time
    ON messages(chat_id, created_at, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender_name, sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_type
    ON messages(message_type);
CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages(thread_id);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    body_text,
    sender_name,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(rowid, body_text, sender_name)
    VALUES (new.id, new.body_text, COALESCE(new.sender_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, body_text, sender_name)
    VALUES ('delete', old.id, old.body_text, COALESCE(old.sender_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, body_text, sender_name)
    VALUES ('delete', old.id, old.body_text, COALESCE(old.sender_name, ''));
    INSERT INTO message_fts(rowid, body_text, sender_name)
    VALUES (new.id, new.body_text, COALESCE(new.sender_name, ''));
END;

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    file_key TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('image', 'file')),
    filename TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    local_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    downloaded_at INTEGER,
    UNIQUE(message_id, file_key, resource_type)
);

CREATE INDEX IF NOT EXISTS idx_attachments_status ON attachments(status);

CREATE TABLE IF NOT EXISTS sync_state (
    container_type TEXT NOT NULL,
    container_id TEXT NOT NULL,
    window_start INTEGER,
    window_end INTEGER,
    page_token TEXT,
    last_message_at INTEGER,
    last_synced_at INTEGER,
    status TEXT NOT NULL DEFAULT 'idle',
    error TEXT,
    PRIMARY KEY (container_type, container_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    requested_days INTEGER,
    chat_ids_json TEXT NOT NULL,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    messages_written INTEGER NOT NULL DEFAULT 0,
    attachments_downloaded INTEGER NOT NULL DEFAULT 0,
    attachments_skipped INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);
"""


class ArchiveDatabase:
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
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            con.executescript(SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            con.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('created_at', ?)",
                (str(int(time.time() * 1000)),),
            )
        os.chmod(self.path, 0o600)

    def integrity_check(self) -> str:
        with self.connection() as con:
            return str(con.execute("PRAGMA integrity_check").fetchone()[0])

    def upsert_conversation(self, item: dict[str, Any]) -> None:
        chat_id = str(item.get("chat_id") or "").strip()
        if not chat_id:
            raise ValueError("conversation 缺少 chat_id")
        raw_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO conversations(
                    chat_id, name, description, chat_mode, chat_type, external,
                    owner_id, avatar_url, status, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    name=CASE WHEN excluded.name='' THEN conversations.name ELSE excluded.name END,
                    description=excluded.description,
                    chat_mode=excluded.chat_mode,
                    chat_type=excluded.chat_type,
                    external=excluded.external,
                    owner_id=COALESCE(excluded.owner_id, conversations.owner_id),
                    avatar_url=COALESCE(excluded.avatar_url, conversations.avatar_url),
                    status=excluded.status,
                    raw_json=excluded.raw_json
                """,
                (
                    chat_id,
                    item.get("name") or item.get("chat_name") or "",
                    item.get("description") or "",
                    item.get("chat_mode") or "unknown",
                    item.get("chat_type") or "unknown",
                    int(bool(item.get("external") or item.get("is_external"))),
                    item.get("owner_id"),
                    item.get("avatar") or item.get("avatar_url"),
                    item.get("status") or "active",
                    raw_json,
                ),
            )

    def ensure_conversation(self, chat_id: str) -> None:
        with self.connection() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations(chat_id, name) VALUES (?, ?)",
                (chat_id, chat_id),
            )

    def upsert_message(self, message: dict[str, Any]) -> bool:
        now = int(time.time() * 1000)
        with self.transaction() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations(chat_id, name) VALUES (?, ?)",
                (message["chat_id"], message["chat_id"]),
            )
            existed = con.execute(
                "SELECT 1 FROM messages WHERE message_id=?", (message["message_id"],)
            ).fetchone() is not None
            con.execute(
                """
                INSERT INTO messages(
                    message_id, chat_id, thread_id, parent_id, root_id, message_type,
                    sender_id, sender_type, sender_name, created_at, updated_at,
                    deleted, recalled, body_text, raw_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    parent_id=excluded.parent_id,
                    root_id=excluded.root_id,
                    message_type=excluded.message_type,
                    sender_id=excluded.sender_id,
                    sender_type=excluded.sender_type,
                    sender_name=excluded.sender_name,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    deleted=excluded.deleted,
                    recalled=excluded.recalled,
                    body_text=excluded.body_text,
                    raw_json=excluded.raw_json,
                    archived_at=excluded.archived_at
                """,
                (
                    message["message_id"],
                    message["chat_id"],
                    message.get("thread_id"),
                    message.get("parent_id"),
                    message.get("root_id"),
                    message.get("message_type") or "unknown",
                    message.get("sender_id"),
                    message.get("sender_type"),
                    message.get("sender_name"),
                    message.get("created_at"),
                    message.get("updated_at"),
                    int(bool(message.get("deleted"))),
                    int(bool(message.get("recalled"))),
                    message.get("body_text") or "",
                    message.get("raw_json") or "{}",
                    now,
                ),
            )
            con.execute(
                """
                UPDATE conversations SET
                    first_message_at=CASE
                        WHEN first_message_at IS NULL THEN ?
                        WHEN ? IS NULL THEN first_message_at
                        ELSE MIN(first_message_at, ?)
                    END,
                    last_message_at=CASE
                        WHEN last_message_at IS NULL THEN ?
                        WHEN ? IS NULL THEN last_message_at
                        ELSE MAX(last_message_at, ?)
                    END
                WHERE chat_id=?
                """,
                (
                    message.get("created_at"),
                    message.get("created_at"),
                    message.get("created_at"),
                    message.get("created_at"),
                    message.get("created_at"),
                    message.get("created_at"),
                    message["chat_id"],
                ),
            )
        return not existed

    def upsert_member(self, chat_id: str, item: dict[str, Any]) -> None:
        member_id = str(item.get("member_id") or item.get("id") or "").strip()
        if not member_id:
            raise ValueError("member 缺少 member_id")
        with self.connection() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations(chat_id, name) VALUES (?, ?)",
                (chat_id, chat_id),
            )
            con.execute(
                """
                INSERT INTO members(chat_id, member_id, name, member_type, tenant_key, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, member_id) DO UPDATE SET
                    name=excluded.name,
                    member_type=excluded.member_type,
                    tenant_key=excluded.tenant_key,
                    raw_json=excluded.raw_json
                """,
                (
                    chat_id,
                    member_id,
                    item.get("name") or "",
                    item.get("member_id_type") or item.get("member_type") or "user",
                    item.get("tenant_key"),
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def member_names(self, chat_id: str) -> dict[str, str]:
        with self.connection() as con:
            rows = con.execute(
                "SELECT member_id, name FROM members WHERE chat_id=? AND name<>''",
                (chat_id,),
            ).fetchall()
        return {str(row["member_id"]): str(row["name"]) for row in rows}

    def ensure_attachment(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        filename: str | None,
    ) -> int:
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO attachments(message_id, file_key, resource_type, filename)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id, file_key, resource_type) DO UPDATE SET
                    filename=COALESCE(excluded.filename, attachments.filename)
                """,
                (message_id, file_key, resource_type, filename),
            )
            row = con.execute(
                """
                SELECT id FROM attachments
                WHERE message_id=? AND file_key=? AND resource_type=?
                """,
                (message_id, file_key, resource_type),
            ).fetchone()
            return int(row["id"])

    def update_attachment(self, attachment_id: int, **values: Any) -> None:
        allowed = {
            "filename",
            "mime_type",
            "byte_size",
            "sha256",
            "local_path",
            "status",
            "error",
            "downloaded_at",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connection() as con:
            con.execute(
                f"UPDATE attachments SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), attachment_id),
            )

    def attachment_bytes(self) -> int:
        with self.connection() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM attachments WHERE status='downloaded'"
            ).fetchone()
            return int(row[0])

    def list_pending_attachments(self) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT a.*, m.chat_id FROM attachments a
                JOIN messages m ON m.message_id=a.message_id
                WHERE a.status IN ('pending', 'error')
                ORDER BY a.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_conversations(self) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT c.*,
                       COUNT(m.id) AS message_count,
                       COUNT(DISTINCT CASE WHEN m.sender_id IS NOT NULL THEN m.sender_id END) AS sender_count
                FROM conversations c
                LEFT JOIN messages m ON m.chat_id=c.chat_id
                GROUP BY c.chat_id
                ORDER BY COALESCE(c.last_message_at, 0) DESC, c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_senders(self, chat_id: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT sender_name FROM messages WHERE sender_name IS NOT NULL AND sender_name<>''"
        params: list[Any] = []
        if chat_id:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY sender_name COLLATE NOCASE"
        with self.connection() as con:
            return [str(row[0]) for row in con.execute(sql, params).fetchall()]

    def query_messages(
        self,
        *,
        chat_id: str | None = None,
        query: str | None = None,
        sender: str | None = None,
        message_type: str | None = None,
        date_from_ms: int | None = None,
        date_to_ms: int | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        joins = []
        where = []
        params: list[Any] = []
        if query and query.strip():
            cleaned_query = query.strip()
            if len(cleaned_query) >= 3:
                joins.append("JOIN message_fts ON message_fts.rowid=m.id")
                where.append("message_fts MATCH ?")
                params.append(_fts_query(cleaned_query))
            else:
                where.append("(m.body_text LIKE ? ESCAPE '\\' OR m.sender_name LIKE ? ESCAPE '\\')")
                like_value = f"%{_like_query(cleaned_query)}%"
                params.extend((like_value, like_value))
        if chat_id:
            where.append("m.chat_id=?")
            params.append(chat_id)
        if sender:
            where.append("m.sender_name=?")
            params.append(sender)
        if message_type:
            where.append("m.message_type=?")
            params.append(message_type)
        if date_from_ms is not None:
            where.append("m.created_at>=?")
            params.append(date_from_ms)
        if date_to_ms is not None:
            where.append("m.created_at<?")
            params.append(date_to_ms)
        predicate = " AND ".join(where) if where else "1=1"
        sql = f"""
            SELECT m.*,
                   c.name AS chat_name,
                   (SELECT COUNT(*) FROM attachments a WHERE a.message_id=m.message_id) AS attachment_count
            FROM messages m
            JOIN conversations c ON c.chat_id=m.chat_id
            {' '.join(joins)}
            WHERE {predicate}
            ORDER BY m.created_at ASC, m.message_id ASC
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

    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        return dict(row) if row else None

    def attachments_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        with self.connection() as con:
            rows = con.execute(
                f"SELECT * FROM attachments WHERE message_id IN ({placeholders}) ORDER BY id",  # noqa: S608
                message_ids,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(str(item["message_id"]), []).append(item)
        return grouped

    def set_sync_state(
        self,
        container_type: str,
        container_id: str,
        *,
        window_start: int | None,
        window_end: int | None,
        page_token: str | None,
        status: str,
        error: str | None = None,
        last_message_at: int | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO sync_state(
                    container_type, container_id, window_start, window_end,
                    page_token, last_message_at, last_synced_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(container_type, container_id) DO UPDATE SET
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    page_token=excluded.page_token,
                    last_message_at=COALESCE(excluded.last_message_at, sync_state.last_message_at),
                    last_synced_at=excluded.last_synced_at,
                    status=excluded.status,
                    error=excluded.error
                """,
                (
                    container_type,
                    container_id,
                    window_start,
                    window_end,
                    page_token,
                    last_message_at,
                    now,
                    status,
                    error,
                ),
            )

    def start_sync_run(self, chat_ids: list[str], requested_days: int) -> int:
        with self.connection() as con:
            cur = con.execute(
                """
                INSERT INTO sync_runs(started_at, requested_days, chat_ids_json)
                VALUES (?, ?, ?)
                """,
                (
                    int(time.time() * 1000),
                    requested_days,
                    json.dumps(chat_ids, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, *, status: str, error: str | None = None, **counts: int) -> None:
        allowed = {
            "messages_seen",
            "messages_written",
            "attachments_downloaded",
            "attachments_skipped",
        }
        values: dict[str, Any] = {
            "finished_at": int(time.time() * 1000),
            "status": status,
            "error": error,
        }
        values.update({key: value for key, value in counts.items() if key in allowed})
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connection() as con:
            con.execute(
                f"UPDATE sync_runs SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), run_id),
            )

    def status(self) -> dict[str, Any]:
        with self.connection() as con:
            counts = {
                "conversations": con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "attachments": con.execute(
                    "SELECT COUNT(*) FROM attachments WHERE status='downloaded'"
                ).fetchone()[0],
                "attachment_bytes": con.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM attachments WHERE status='downloaded'"
                ).fetchone()[0],
            }
            latest = con.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {**counts, "latest_sync": dict(latest) if latest else None}


def _fts_query(value: str) -> str:
    cleaned = value.strip().replace('"', '""')
    if not cleaned:
        raise ValueError("搜索词不能为空")
    return f'"{cleaned}"'


def _like_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
