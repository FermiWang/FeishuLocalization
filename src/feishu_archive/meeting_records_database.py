from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, time as day_time
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    cursor INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'never',
    started_at INTEGER,
    finished_at INTEGER,
    error TEXT,
    events_applied INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO sync_state(singleton) VALUES (1);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL,
    cursor_before INTEGER NOT NULL,
    cursor_after INTEGER,
    events_applied INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS records (
    meeting_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    meeting_date TEXT NOT NULL,
    title TEXT NOT NULL,
    background TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    editor_kind TEXT NOT NULL DEFAULT '',
    event_seq INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meeting_records_day ON records(meeting_date, deleted);
CREATE TABLE IF NOT EXISTS sections (
    meeting_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    section_id TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY(meeting_id, section_id),
    FOREIGN KEY(meeting_id) REFERENCES records(meeting_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stale_dates (
    meeting_date TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL,
    meeting_snapshot_hash TEXT NOT NULL,
    detected_at INTEGER NOT NULL,
    refreshed_at INTEGER,
    insights_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS report_snapshots (
    meeting_date TEXT NOT NULL,
    insights_run_id INTEGER NOT NULL,
    meeting_snapshot_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(meeting_date, insights_run_id)
);
"""


class MeetingRecordsDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"会议记录数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}"
                )
            con.executescript(SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(self.path, 0o600)

    def status(self) -> dict[str, Any]:
        with self.connection() as con:
            state = dict(con.execute("SELECT * FROM sync_state WHERE singleton=1").fetchone())
            state["records"] = int(con.execute(
                "SELECT COUNT(*) FROM records WHERE deleted=0"
            ).fetchone()[0])
            state["sections"] = int(con.execute(
                "SELECT COUNT(*) FROM sections s JOIN records r ON r.meeting_id=s.meeting_id WHERE r.deleted=0"
            ).fetchone()[0])
            state["pending_refresh_dates"] = int(con.execute(
                "SELECT COUNT(*) FROM stale_dates WHERE status='pending'"
            ).fetchone()[0])
            return state

    def cursor(self) -> int:
        with self.connection() as con:
            return int(con.execute("SELECT cursor FROM sync_state WHERE singleton=1").fetchone()[0])

    def start_sync(self, trigger: str) -> int:
        now = int(time.time() * 1000)
        with self.connection() as con:
            cursor = int(con.execute("SELECT cursor FROM sync_state WHERE singleton=1").fetchone()[0])
            run = con.execute(
                "INSERT INTO sync_runs(trigger,started_at,status,cursor_before) VALUES (?,?,'running',?)",
                (trigger, now, cursor),
            )
            con.execute(
                "UPDATE sync_state SET status='running',started_at=?,finished_at=NULL,error=NULL WHERE singleton=1",
                (now,),
            )
            return int(run.lastrowid)

    def finish_sync(self, run_id: int, *, status: str, cursor: int,
                    events_applied: int, error: str | None = None) -> None:
        now = int(time.time() * 1000)
        with self.connection() as con:
            con.execute(
                """UPDATE sync_runs SET finished_at=?,status=?,cursor_after=?,events_applied=?,error=?
                   WHERE id=?""",
                (now, status, cursor, events_applied, error, run_id),
            )
            con.execute(
                """UPDATE sync_state SET cursor=?,status=?,finished_at=?,error=?,
                   events_applied=events_applied+? WHERE singleton=1""",
                (cursor, status, now, error, events_applied),
            )

    def apply_events(self, events: list[dict[str, Any]], *, report_dates: set[str]) -> int:
        applied = 0
        with self.connection() as con:
            for event in events:
                seq = int(event["seq"])
                meeting_id = int(event["meeting_id"])
                event_type = str(event["event_type"])
                payload = dict(event.get("payload") or {})
                current = con.execute(
                    "SELECT * FROM records WHERE meeting_id=?", (meeting_id,)
                ).fetchone()
                if current is not None and seq <= int(current["event_seq"]):
                    continue
                old_date = str(current["meeting_date"]) if current else ""
                if event_type == "delete":
                    if current is None:
                        con.execute(
                            """INSERT INTO records(meeting_id,revision,meeting_date,title,content_hash,
                               structured_json,event_seq,deleted,updated_at) VALUES (?,?,?,?,?,'{}',?,1,?)""",
                            (meeting_id, int(event.get("revision") or 0),
                             str(event.get("meeting_date") or payload.get("meeting_date") or ""),
                             str(payload.get("title") or "已删除会议"), str(event["content_hash"]),
                             seq, int(time.time() * 1000)),
                        )
                    else:
                        con.execute(
                            "UPDATE records SET deleted=1,event_seq=?,content_hash=?,updated_at=? WHERE meeting_id=?",
                            (seq, str(event["content_hash"]), int(time.time() * 1000), meeting_id),
                        )
                        con.execute("DELETE FROM sections WHERE meeting_id=?", (meeting_id,))
                    new_date = str(event.get("meeting_date") or payload.get("meeting_date") or old_date)
                elif event_type == "upsert":
                    structured = dict(payload.get("structured") or {})
                    meeting = dict(payload.get("meeting") or {})
                    new_date = str(meeting.get("meeting_date") or event.get("meeting_date") or "")
                    con.execute(
                        """INSERT INTO records
                           (meeting_id,revision,meeting_date,title,background,content_hash,structured_json,
                            model_id,prompt_version,editor_kind,event_seq,deleted,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)
                           ON CONFLICT(meeting_id) DO UPDATE SET revision=excluded.revision,
                            meeting_date=excluded.meeting_date,title=excluded.title,background=excluded.background,
                            content_hash=excluded.content_hash,structured_json=excluded.structured_json,
                            model_id=excluded.model_id,prompt_version=excluded.prompt_version,
                            editor_kind=excluded.editor_kind,event_seq=excluded.event_seq,deleted=0,
                            updated_at=excluded.updated_at""",
                        (
                            meeting_id, int(payload.get("revision") or event.get("revision") or 0),
                            new_date, str(meeting.get("title") or structured.get("title") or "未命名会议"),
                            str(meeting.get("background") or ""), str(payload.get("content_hash") or event["content_hash"]),
                            json.dumps(structured, ensure_ascii=False, sort_keys=True),
                            str(payload.get("model_id") or ""), str(payload.get("prompt_version") or ""),
                            str(payload.get("editor_kind") or ""), seq, int(time.time() * 1000),
                        ),
                    )
                    con.execute("DELETE FROM sections WHERE meeting_id=?", (meeting_id,))
                    for position, section in enumerate(structured.get("sections") or []):
                        if not isinstance(section, dict):
                            continue
                        con.execute(
                            """INSERT INTO sections
                               (meeting_id,revision,section_id,title,kind,content,source_refs_json,position)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (
                                meeting_id, int(payload.get("revision") or event.get("revision") or 0),
                                str(section.get("id") or f"section-{position+1}"),
                                str(section.get("title") or "未命名章节"),
                                str(section.get("kind") or "prose"), str(section.get("content") or ""),
                                json.dumps(section.get("source_refs") or [], ensure_ascii=False), position,
                            ),
                        )
                else:
                    raise ValueError(f"unknown meeting sync event: {event_type}")
                for affected_date in {old_date, new_date} - {""}:
                    if affected_date in report_dates:
                        snapshot = self._snapshot_hash_with_connection(con, affected_date)
                        con.execute(
                            """INSERT INTO stale_dates
                               (meeting_date,status,reason,meeting_snapshot_hash,detected_at)
                               VALUES (?,'pending','会议证据已更新，待人工刷新',?,?)
                               ON CONFLICT(meeting_date) DO UPDATE SET status='pending',reason=excluded.reason,
                               meeting_snapshot_hash=excluded.meeting_snapshot_hash,
                               detected_at=excluded.detected_at,refreshed_at=NULL,insights_run_id=NULL""",
                            (affected_date, snapshot, int(time.time() * 1000)),
                        )
                applied += 1
        return applied

    def _snapshot_hash_with_connection(self, con: sqlite3.Connection, meeting_date: str) -> str:
        rows = con.execute(
            """SELECT meeting_id,revision,content_hash,deleted FROM records
               WHERE meeting_date=? ORDER BY meeting_id""",
            (meeting_date,),
        ).fetchall()
        raw = json.dumps([dict(row) for row in rows], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def snapshot_hash(self, meeting_date: str) -> str:
        with self.connection() as con:
            return self._snapshot_hash_with_connection(con, meeting_date)

    def stale_status(self, meeting_date: str | None = None) -> dict[str, Any]:
        with self.connection() as con:
            if meeting_date:
                row = con.execute("SELECT * FROM stale_dates WHERE meeting_date=?", (meeting_date,)).fetchone()
                return dict(row) if row else {"meeting_date": meeting_date, "status": "current"}
            rows = con.execute(
                "SELECT * FROM stale_dates WHERE status='pending' ORDER BY meeting_date DESC"
            ).fetchall()
            return {"items": [dict(row) for row in rows], "count": len(rows)}

    def mark_refreshed(self, meeting_date: str, insights_run_id: int) -> None:
        now = int(time.time() * 1000)
        with self.connection() as con:
            snapshot = self._snapshot_hash_with_connection(con, meeting_date)
            con.execute(
                """INSERT OR REPLACE INTO report_snapshots
                   (meeting_date,insights_run_id,meeting_snapshot_hash,created_at) VALUES (?,?,?,?)""",
                (meeting_date, insights_run_id, snapshot, now),
            )
            con.execute(
                """INSERT INTO stale_dates
                   (meeting_date,status,reason,meeting_snapshot_hash,detected_at,refreshed_at,insights_run_id)
                   VALUES (?,'current','',?,?,?,?)
                   ON CONFLICT(meeting_date) DO UPDATE SET status='current',reason='',
                   meeting_snapshot_hash=excluded.meeting_snapshot_hash,
                   refreshed_at=excluded.refreshed_at,insights_run_id=excluded.insights_run_id""",
                (meeting_date, snapshot, now, now, insights_run_id),
            )

    def evidence_for_day(self, meeting_date: str, timezone: str | ZoneInfo) -> list[dict[str, Any]]:
        zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        event_ms = int(datetime.combine(date.fromisoformat(meeting_date), day_time(12), tzinfo=zone).timestamp() * 1000)
        with self.connection() as con:
            rows = con.execute(
                """SELECT r.meeting_id,r.revision,r.title,r.background,r.model_id,r.prompt_version,
                          s.section_id,s.title section_title,s.kind,s.content,s.source_refs_json,s.position
                   FROM records r JOIN sections s ON s.meeting_id=r.meeting_id
                   WHERE r.meeting_date=? AND r.deleted=0 ORDER BY r.meeting_id,s.position""",
                (meeting_date,),
            ).fetchall()
        evidence: list[dict[str, Any]] = []
        for row in rows:
            parts = self._section_parts(
                str(row["section_id"]), str(row["section_title"]), str(row["content"])
            )
            for part_id, part_title, content in parts:
                evidence_id = f"meeting:{row['meeting_id']}:r{row['revision']}:{part_id}"
                evidence.append({
                    "evidence_id": evidence_id,
                    "source_kind": "meeting",
                    "source_id": f"{row['meeting_id']}:r{row['revision']}:{part_id}",
                    "thread_key": f"meeting:{row['meeting_id']}",
                    "created_at": event_ms,
                    "updated_at": event_ms,
                    "sort_time": event_ms,
                    "direction": "internal",
                    "sender_name": "详细会议记录",
                    "title": f"{row['title']}｜{part_title}",
                    "text": f"会议：{row['title']}\n章节：{part_title}\n{content}",
                    "citation": evidence_id,
                    "metadata": {
                        "meeting_id": int(row["meeting_id"]), "revision": int(row["revision"]),
                        "section_id": str(row["section_id"]), "section_part_id": part_id,
                        "source_refs": json.loads(row["source_refs_json"]),
                        "model_id": row["model_id"], "prompt_version": row["prompt_version"],
                    },
                })
        return evidence

    @staticmethod
    def _section_parts(
        section_id: str, section_title: str, content: str, limit: int = 10_000
    ) -> list[tuple[str, str, str]]:
        if len(content) <= limit:
            return [(section_id, section_title, content)]
        paragraphs = content.split("\n")
        parts: list[str] = []
        buffer: list[str] = []
        size = 0
        for paragraph in paragraphs:
            while len(paragraph) > limit:
                if buffer:
                    parts.append("\n".join(buffer))
                    buffer, size = [], 0
                parts.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            if not paragraph:
                continue
            if buffer and size + len(paragraph) + 1 > limit:
                parts.append("\n".join(buffer))
                buffer, size = [], 0
            buffer.append(paragraph)
            size += len(paragraph) + 1
        if buffer:
            parts.append("\n".join(buffer))
        return [(f"{section_id}-{index:02d}", f"{section_title}（{index}/{len(parts)}）", value)
                for index, value in enumerate(parts, 1)]

    def history_bounds(self) -> dict[str, Any]:
        with self.connection() as con:
            row = con.execute(
                """SELECT MIN(meeting_date) earliest,MAX(meeting_date) latest,COUNT(*) count
                   FROM records WHERE deleted=0 AND meeting_date<>''"""
            ).fetchone()
            return {
                "earliest_date": row["earliest"], "latest_date": row["latest"],
                "observed_records": int(row["count"] or 0),
            }
