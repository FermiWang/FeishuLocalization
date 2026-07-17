from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "3"


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

CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    conversations_discovered INTEGER NOT NULL DEFAULT 0,
    new_conversations INTEGER NOT NULL DEFAULT 0,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    messages_written INTEGER NOT NULL DEFAULT 0,
    attachments_downloaded INTEGER NOT NULL DEFAULT 0,
    attachments_skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS wiki_spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    space_type TEXT NOT NULL DEFAULT 'unknown',
    visibility TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_at INTEGER,
    last_synced_at INTEGER,
    error TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS wiki_nodes (
    node_token TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES wiki_spaces(space_id) ON DELETE CASCADE,
    obj_token TEXT NOT NULL,
    obj_type TEXT NOT NULL DEFAULT 'unknown',
    parent_node_token TEXT,
    node_type TEXT NOT NULL DEFAULT 'origin',
    origin_node_token TEXT,
    origin_space_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    has_child INTEGER NOT NULL DEFAULT 0 CHECK (has_child IN (0, 1)),
    position INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL DEFAULT '',
    obj_create_time INTEGER,
    obj_edit_time INTEGER,
    node_create_time INTEGER,
    creator TEXT,
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_at INTEGER,
    last_synced_at INTEGER,
    error TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_wiki_nodes_space_parent
    ON wiki_nodes(space_id, parent_node_token, position, title);
CREATE INDEX IF NOT EXISTS idx_wiki_nodes_obj
    ON wiki_nodes(obj_token, obj_type);

CREATE TABLE IF NOT EXISTS wiki_documents (
    id INTEGER PRIMARY KEY,
    obj_token TEXT NOT NULL UNIQUE,
    obj_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    revision_id INTEGER,
    source_edit_time INTEGER,
    content_text TEXT NOT NULL DEFAULT '',
    rendered_html TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT,
    local_export_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    last_synced_at INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_document_fts USING fts5(
    title,
    content_text,
    content='wiki_documents',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS wiki_documents_ai AFTER INSERT ON wiki_documents BEGIN
    INSERT INTO wiki_document_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS wiki_documents_ad AFTER DELETE ON wiki_documents BEGIN
    INSERT INTO wiki_document_fts(wiki_document_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS wiki_documents_au AFTER UPDATE ON wiki_documents BEGIN
    INSERT INTO wiki_document_fts(wiki_document_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
    INSERT INTO wiki_document_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;

CREATE TABLE IF NOT EXISTS wiki_blocks (
    obj_token TEXT NOT NULL REFERENCES wiki_documents(obj_token) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    parent_id TEXT,
    block_type INTEGER,
    position INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (obj_token, block_id)
);

CREATE INDEX IF NOT EXISTS idx_wiki_blocks_document_position
    ON wiki_blocks(obj_token, position);

CREATE TABLE IF NOT EXISTS wiki_assets (
    id INTEGER PRIMARY KEY,
    obj_token TEXT NOT NULL REFERENCES wiki_documents(obj_token) ON DELETE CASCADE,
    block_id TEXT,
    file_token TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('image', 'file')),
    filename TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    local_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    downloaded_at INTEGER,
    UNIQUE(obj_token, file_token, asset_type)
);

CREATE INDEX IF NOT EXISTS idx_wiki_assets_status ON wiki_assets(status);

CREATE TABLE IF NOT EXISTS wiki_sync_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    requested_space_ids_json TEXT NOT NULL DEFAULT '[]',
    spaces_seen INTEGER NOT NULL DEFAULT 0,
    nodes_seen INTEGER NOT NULL DEFAULT 0,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_written INTEGER NOT NULL DEFAULT 0,
    assets_downloaded INTEGER NOT NULL DEFAULT 0,
    assets_skipped INTEGER NOT NULL DEFAULT 0,
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

    def conversation_ids(self, chat_mode: str | None = None) -> list[str]:
        with self.connection() as con:
            if chat_mode is None:
                rows = con.execute(
                    "SELECT chat_id FROM conversations "
                    "ORDER BY COALESCE(last_message_at, 0) DESC, chat_id"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT chat_id FROM conversations WHERE chat_mode=? "
                    "ORDER BY COALESCE(last_message_at, 0) DESC, chat_id",
                    (chat_mode,),
                ).fetchall()
        return [str(row["chat_id"]) for row in rows]

    def get_sync_state(self, container_type: str, container_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM sync_state WHERE container_type=? AND container_id=?",
                (container_type, container_id),
            ).fetchone()
        return dict(row) if row else None

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

    def list_attachments_by_sender(
        self,
        sender_id: str,
        *,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connection() as con:
            if resource_type is None:
                rows = con.execute(
                    """
                    SELECT a.*, m.chat_id FROM attachments a
                    JOIN messages m ON m.message_id=a.message_id
                    WHERE m.sender_id=?
                    ORDER BY a.id
                    """,
                    (sender_id,),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                SELECT a.*, m.chat_id FROM attachments a
                JOIN messages m ON m.message_id=a.message_id
                WHERE m.sender_id=? AND a.resource_type=?
                ORDER BY a.id
                """,
                    (sender_id, resource_type),
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_attachments(self, attachment_ids: list[int]) -> None:
        if not attachment_ids:
            return
        placeholders = ",".join("?" for _ in attachment_ids)
        with self.connection() as con:
            con.execute(
                f"DELETE FROM attachments WHERE id IN ({placeholders})",  # noqa: S608
                attachment_ids,
            )

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
        newest_first: bool = True,
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
        direction = "DESC" if newest_first else "ASC"
        sql = f"""
            SELECT m.*,
                   c.name AS chat_name,
                   (SELECT COUNT(*) FROM attachments a
                    WHERE a.message_id=m.message_id AND a.resource_type='image') AS image_count,
                   (SELECT COUNT(*) FROM attachments a
                    WHERE a.message_id=m.message_id AND a.resource_type='file') AS attachment_count
            FROM messages m
            JOIN conversations c ON c.chat_id=m.chat_id
            {' '.join(joins)}
            WHERE {predicate}
            ORDER BY m.created_at {direction}, m.message_id {direction}
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

    def resources_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
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

    def attachments_for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return self.resources_for_messages(message_ids)

    def upsert_wiki_space(self, item: dict[str, Any], *, seen_at: int | None = None) -> None:
        space_id = str(item.get("space_id") or "").strip()
        if not space_id:
            raise ValueError("知识空间缺少 space_id")
        seen_at = seen_at or int(time.time() * 1000)
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO wiki_spaces(
                    space_id, name, description, space_type, visibility,
                    status, last_seen_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(space_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    space_type=excluded.space_type,
                    visibility=excluded.visibility,
                    status='active',
                    last_seen_at=excluded.last_seen_at,
                    error=NULL,
                    raw_json=excluded.raw_json
                """,
                (
                    space_id,
                    item.get("name") or "",
                    item.get("description") or "",
                    item.get("space_type") or item.get("type") or "unknown",
                    item.get("visibility") or "unknown",
                    seen_at,
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def mark_unseen_wiki_spaces(self, seen_at: int) -> None:
        with self.connection() as con:
            con.execute(
                "UPDATE wiki_spaces SET status='missing' "
                "WHERE last_seen_at IS NULL OR last_seen_at<>?",
                (seen_at,),
            )

    def update_wiki_space_sync(
        self,
        space_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        with self.connection() as con:
            con.execute(
                """
                UPDATE wiki_spaces
                SET status=?, error=?, last_synced_at=?
                WHERE space_id=?
                """,
                (status, error, int(time.time() * 1000), space_id),
            )

    def list_wiki_spaces(self, *, include_missing: bool = False) -> list[dict[str, Any]]:
        where = "" if include_missing else "WHERE s.status<>'missing'"
        with self.connection() as con:
            rows = con.execute(
                f"""
                SELECT s.*,
                       COUNT(n.node_token) AS node_count,
                       SUM(CASE WHEN d.status='synced' THEN 1 ELSE 0 END) AS synced_document_count
                FROM wiki_spaces s
                LEFT JOIN wiki_nodes n ON n.space_id=s.space_id AND n.status<>'missing'
                LEFT JOIN wiki_documents d ON d.obj_token=n.obj_token
                {where}
                GROUP BY s.space_id
                ORDER BY s.name COLLATE NOCASE, s.space_id
                """  # noqa: S608
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_wiki_node(
        self,
        item: dict[str, Any],
        *,
        space_id: str,
        parent_node_token: str | None,
        path: str,
        position: int,
        seen_at: int,
    ) -> None:
        node_token = str(item.get("node_token") or "").strip()
        obj_token = str(item.get("obj_token") or "").strip()
        if not node_token or not obj_token:
            raise ValueError("知识库节点缺少 node_token 或 obj_token")
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO wiki_nodes(
                    node_token, space_id, obj_token, obj_type, parent_node_token,
                    node_type, origin_node_token, origin_space_id, title, has_child,
                    position, path, obj_create_time, obj_edit_time, node_create_time,
                    creator, owner, status, last_seen_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(node_token) DO UPDATE SET
                    space_id=excluded.space_id,
                    obj_token=excluded.obj_token,
                    obj_type=excluded.obj_type,
                    parent_node_token=excluded.parent_node_token,
                    node_type=excluded.node_type,
                    origin_node_token=excluded.origin_node_token,
                    origin_space_id=excluded.origin_space_id,
                    title=excluded.title,
                    has_child=excluded.has_child,
                    position=excluded.position,
                    path=excluded.path,
                    obj_create_time=excluded.obj_create_time,
                    obj_edit_time=excluded.obj_edit_time,
                    node_create_time=excluded.node_create_time,
                    creator=excluded.creator,
                    owner=excluded.owner,
                    status='active',
                    last_seen_at=excluded.last_seen_at,
                    error=NULL,
                    raw_json=excluded.raw_json
                """,
                (
                    node_token,
                    space_id,
                    obj_token,
                    item.get("obj_type") or "unknown",
                    parent_node_token,
                    item.get("node_type") or "origin",
                    item.get("origin_node_token"),
                    item.get("origin_space_id"),
                    item.get("title") or "",
                    int(bool(item.get("has_child"))),
                    int(position),
                    path,
                    _optional_int(item.get("obj_create_time")),
                    _optional_int(item.get("obj_edit_time")),
                    _optional_int(item.get("node_create_time")),
                    _identity_value(item.get("creator")),
                    _identity_value(item.get("owner")),
                    seen_at,
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def mark_unseen_wiki_nodes(self, space_id: str, seen_at: int) -> None:
        with self.connection() as con:
            con.execute(
                """
                UPDATE wiki_nodes SET status='missing'
                WHERE space_id=? AND (last_seen_at IS NULL OR last_seen_at<>?)
                """,
                (space_id, seen_at),
            )

    def list_wiki_nodes(self, space_id: str) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT n.*, d.status AS document_status, d.error AS document_error,
                       d.last_synced_at AS document_synced_at
                FROM wiki_nodes n
                LEFT JOIN wiki_documents d ON d.obj_token=n.obj_token
                WHERE n.space_id=? AND n.status<>'missing'
                ORDER BY n.path COLLATE NOCASE, n.position, n.title COLLATE NOCASE
                """,
                (space_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_wiki_node(self, node_token: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM wiki_nodes WHERE node_token=?", (node_token,)
            ).fetchone()
        return dict(row) if row else None

    def get_wiki_document(self, obj_token: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM wiki_documents WHERE obj_token=?", (obj_token,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_wiki_document(self, item: dict[str, Any]) -> bool:
        obj_token = str(item.get("obj_token") or "").strip()
        if not obj_token:
            raise ValueError("知识库文档缺少 obj_token")
        with self.transaction() as con:
            previous = con.execute(
                "SELECT content_sha256, status, error FROM wiki_documents WHERE obj_token=?",
                (obj_token,),
            ).fetchone()
            con.execute(
                """
                INSERT INTO wiki_documents(
                    obj_token, obj_type, title, revision_id, source_edit_time,
                    content_text, rendered_html, content_sha256, local_export_path,
                    status, error, last_synced_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(obj_token) DO UPDATE SET
                    obj_type=excluded.obj_type,
                    title=excluded.title,
                    revision_id=excluded.revision_id,
                    source_edit_time=excluded.source_edit_time,
                    content_text=excluded.content_text,
                    rendered_html=excluded.rendered_html,
                    content_sha256=excluded.content_sha256,
                    local_export_path=COALESCE(excluded.local_export_path, wiki_documents.local_export_path),
                    status=excluded.status,
                    error=excluded.error,
                    last_synced_at=excluded.last_synced_at,
                    raw_json=excluded.raw_json
                """,
                (
                    obj_token,
                    item.get("obj_type") or "unknown",
                    item.get("title") or "",
                    _optional_int(item.get("revision_id")),
                    _optional_int(item.get("source_edit_time")),
                    item.get("content_text") or "",
                    item.get("rendered_html") or "",
                    item.get("content_sha256"),
                    item.get("local_export_path"),
                    item.get("status") or "pending",
                    item.get("error"),
                    _optional_int(item.get("last_synced_at")) or int(time.time() * 1000),
                    json.dumps(item.get("raw_json") or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        if previous is None:
            return True
        return (
            str(previous["content_sha256"] or "") != str(item.get("content_sha256") or "")
            or str(previous["status"] or "") != str(item.get("status") or "")
            or str(previous["error"] or "") != str(item.get("error") or "")
        )

    def mark_wiki_document_error(self, item: dict[str, Any], error: str) -> None:
        obj_token = str(item.get("obj_token") or "").strip()
        if not obj_token:
            return
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO wiki_documents(
                    obj_token, obj_type, title, source_edit_time,
                    status, error, last_synced_at, raw_json
                ) VALUES (?, ?, ?, ?, 'error', ?, ?, ?)
                ON CONFLICT(obj_token) DO UPDATE SET
                    obj_type=excluded.obj_type,
                    title=CASE WHEN excluded.title='' THEN wiki_documents.title ELSE excluded.title END,
                    source_edit_time=COALESCE(excluded.source_edit_time, wiki_documents.source_edit_time),
                    status='error',
                    error=excluded.error,
                    last_synced_at=excluded.last_synced_at,
                    raw_json=excluded.raw_json
                """,
                (
                    obj_token,
                    item.get("obj_type") or "unknown",
                    item.get("title") or "",
                    _optional_int(item.get("obj_edit_time")),
                    error,
                    int(time.time() * 1000),
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def replace_wiki_blocks(self, obj_token: str, blocks: list[dict[str, Any]]) -> None:
        with self.transaction() as con:
            con.execute("DELETE FROM wiki_blocks WHERE obj_token=?", (obj_token,))
            con.executemany(
                """
                INSERT INTO wiki_blocks(
                    obj_token, block_id, parent_id, block_type, position, text, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        obj_token,
                        str(block.get("block_id") or f"position-{position}"),
                        block.get("parent_id"),
                        _optional_int(block.get("block_type")),
                        position,
                        block.get("text") or "",
                        json.dumps(block.get("raw_json") or block, ensure_ascii=False, separators=(",", ":")),
                    )
                    for position, block in enumerate(blocks)
                ],
            )

    def ensure_wiki_asset(
        self,
        obj_token: str,
        file_token: str,
        asset_type: str,
        *,
        block_id: str | None = None,
        filename: str | None = None,
    ) -> int:
        with self.connection() as con:
            con.execute(
                """
                INSERT INTO wiki_assets(obj_token, block_id, file_token, asset_type, filename)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(obj_token, file_token, asset_type) DO UPDATE SET
                    block_id=COALESCE(excluded.block_id, wiki_assets.block_id),
                    filename=COALESCE(excluded.filename, wiki_assets.filename)
                """,
                (obj_token, block_id, file_token, asset_type, filename),
            )
            row = con.execute(
                """
                SELECT id FROM wiki_assets
                WHERE obj_token=? AND file_token=? AND asset_type=?
                """,
                (obj_token, file_token, asset_type),
            ).fetchone()
        return int(row["id"])

    def update_wiki_asset(self, asset_id: int, **values: Any) -> None:
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
                f"UPDATE wiki_assets SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), asset_id),
            )

    def get_wiki_asset(self, asset_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT * FROM wiki_assets WHERE id=?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def list_wiki_assets(self, obj_token: str) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                "SELECT * FROM wiki_assets WHERE obj_token=? ORDER BY id", (obj_token,)
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_wiki_assets(
        self,
        obj_token: str,
        active_assets: set[tuple[str, str]],
    ) -> None:
        with self.connection() as con:
            rows = con.execute(
                "SELECT id, file_token, asset_type FROM wiki_assets WHERE obj_token=?",
                (obj_token,),
            ).fetchall()
            stale_ids = [
                int(row["id"])
                for row in rows
                if (str(row["file_token"]), str(row["asset_type"])) not in active_assets
            ]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                con.execute(
                    f"DELETE FROM wiki_assets WHERE id IN ({placeholders})",  # noqa: S608
                    stale_ids,
                )

    def wiki_asset_bytes(self) -> int:
        with self.connection() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM wiki_assets WHERE status='downloaded'"
            ).fetchone()
        return int(row[0])

    def wiki_document_for_node(self, node_token: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                """
                SELECT n.node_token, n.space_id, n.parent_node_token, n.path,
                       n.node_type, n.obj_create_time, n.obj_edit_time,
                       d.id,
                       COALESCE(d.obj_token, n.obj_token) AS obj_token,
                       COALESCE(d.obj_type, n.obj_type) AS obj_type,
                       COALESCE(NULLIF(d.title, ''), n.title) AS title,
                       d.revision_id, d.source_edit_time, d.content_text,
                       d.rendered_html, d.content_sha256, d.local_export_path,
                       COALESCE(d.status, 'pending') AS status,
                       d.error, d.last_synced_at, d.raw_json
                FROM wiki_nodes n
                LEFT JOIN wiki_documents d ON d.obj_token=n.obj_token
                WHERE n.node_token=?
                """,
                (node_token,),
            ).fetchone()
        return dict(row) if row else None

    def search_wiki_documents(
        self,
        query: str,
        *,
        space_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cleaned_query = query.strip()
        if not cleaned_query:
            return []
        limit = max(1, min(int(limit), 200))
        params: list[Any] = []
        joins = ""
        where = ["n.status<>'missing'"]
        if len(cleaned_query) >= 3:
            joins = "JOIN wiki_document_fts ON wiki_document_fts.rowid=d.id"
            where.append("wiki_document_fts MATCH ?")
            params.append(_fts_query(cleaned_query))
        else:
            where.append("(d.title LIKE ? ESCAPE '\\' OR d.content_text LIKE ? ESCAPE '\\')")
            like_value = f"%{_like_query(cleaned_query)}%"
            params.extend((like_value, like_value))
        if space_id:
            where.append("n.space_id=?")
            params.append(space_id)
        params.append(limit)
        sql = f"""
            SELECT n.node_token, n.space_id, n.path, n.obj_type, n.obj_edit_time,
                   d.obj_token, d.title, d.status, d.error, d.last_synced_at,
                   substr(d.content_text, 1, 500) AS excerpt
            FROM wiki_documents d
            JOIN wiki_nodes n ON n.obj_token=d.obj_token
            {joins}
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(d.source_edit_time, n.obj_edit_time, 0) DESC,
                     d.title COLLATE NOCASE
            LIMIT ?
        """
        try:
            with self.connection() as con:
                rows = con.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                raise ValueError("搜索词包含 FTS5 无法解析的语法") from exc
            raise
        return [dict(row) for row in rows]

    def start_wiki_sync_run(self, trigger: str, space_ids: list[str]) -> int:
        now = int(time.time() * 1000)
        with self.transaction() as con:
            con.execute(
                """
                UPDATE wiki_sync_runs SET finished_at=?, status='error',
                    error=COALESCE(error, '上次知识库同步任务异常中断')
                WHERE status='running'
                """,
                (now,),
            )
            cur = con.execute(
                """
                INSERT INTO wiki_sync_runs(trigger, started_at, requested_space_ids_json)
                VALUES (?, ?, ?)
                """,
                (trigger, now, json.dumps(space_ids, ensure_ascii=False)),
            )
        return int(cur.lastrowid)

    def finish_wiki_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        **counts: int,
    ) -> None:
        allowed = {
            "spaces_seen",
            "nodes_seen",
            "documents_seen",
            "documents_written",
            "assets_downloaded",
            "assets_skipped",
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
                f"UPDATE wiki_sync_runs SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), run_id),
            )

    def latest_wiki_sync_run(self) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM wiki_sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def wiki_status(self) -> dict[str, Any]:
        with self.connection() as con:
            status = {
                "spaces": con.execute(
                    "SELECT COUNT(*) FROM wiki_spaces WHERE status<>'missing'"
                ).fetchone()[0],
                "nodes": con.execute(
                    "SELECT COUNT(*) FROM wiki_nodes WHERE status<>'missing'"
                ).fetchone()[0],
                "documents": con.execute(
                    "SELECT COUNT(*) FROM wiki_documents"
                ).fetchone()[0],
                "synced_documents": con.execute(
                    "SELECT COUNT(*) FROM wiki_documents WHERE status='synced'"
                ).fetchone()[0],
                "metadata_only_documents": con.execute(
                    "SELECT COUNT(*) FROM wiki_documents WHERE status='metadata_only'"
                ).fetchone()[0],
                "failed_documents": con.execute(
                    "SELECT COUNT(*) FROM wiki_documents WHERE status='error'"
                ).fetchone()[0],
                "assets": con.execute(
                    "SELECT COUNT(*) FROM wiki_assets WHERE status='downloaded'"
                ).fetchone()[0],
                "asset_bytes": con.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM wiki_assets "
                    "WHERE status='downloaded'"
                ).fetchone()[0],
            }
            latest = con.execute(
                "SELECT * FROM wiki_sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        status["latest_sync"] = dict(latest) if latest else None
        return status

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

    def start_sync_run(self, chat_ids: list[str], requested_days: int | None) -> int:
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

    def start_sync_job(self, trigger: str) -> int:
        now = int(time.time() * 1000)
        with self.transaction() as con:
            con.execute(
                """
                UPDATE sync_jobs
                SET finished_at=?, status='error',
                    error=COALESCE(error, '上次同步任务异常中断')
                WHERE status='running'
                """,
                (now,),
            )
            cur = con.execute(
                "INSERT INTO sync_jobs(trigger, started_at) VALUES (?, ?)",
                (trigger, now),
            )
            return int(cur.lastrowid)

    def finish_sync_job(
        self,
        job_id: int,
        *,
        status: str,
        error: str | None = None,
        **counts: int,
    ) -> None:
        allowed = {
            "conversations_discovered",
            "new_conversations",
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
                f"UPDATE sync_jobs SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), job_id),
            )

    def latest_sync_job(self) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def status(self) -> dict[str, Any]:
        with self.connection() as con:
            counts = {
                "conversations": con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "images": con.execute(
                    "SELECT COUNT(*) FROM attachments "
                    "WHERE status='downloaded' AND resource_type='image'"
                ).fetchone()[0],
                "image_bytes": con.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM attachments "
                    "WHERE status='downloaded' AND resource_type='image'"
                ).fetchone()[0],
                "attachments": con.execute(
                    "SELECT COUNT(*) FROM attachments "
                    "WHERE status='downloaded' AND resource_type='file'"
                ).fetchone()[0],
                "attachment_bytes": con.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM attachments "
                    "WHERE status='downloaded' AND resource_type='file'"
                ).fetchone()[0],
                "resource_bytes": con.execute(
                    "SELECT COALESCE(SUM(byte_size), 0) FROM attachments WHERE status='downloaded'"
                ).fetchone()[0],
            }
            latest = con.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest_job = con.execute(
                "SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            **counts,
            "latest_sync": dict(latest) if latest else None,
            "latest_sync_job": dict(latest_job) if latest_job else None,
        }


def _fts_query(value: str) -> str:
    cleaned = value.strip().replace('"', '""')
    if not cleaned:
        raise ValueError("搜索词不能为空")
    return f'"{cleaned}"'


def _like_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _identity_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("id", "open_id", "user_id", "union_id"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)
