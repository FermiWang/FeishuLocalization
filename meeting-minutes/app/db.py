"""SQLite storage for meetings, ordered sources, recoverable jobs and revisions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(
    os.environ.get(
        "MEETING_MINUTES_DATA_DIR",
        str(Path(__file__).resolve().parent.parent / "data"),
    )
).expanduser().resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "meetings.db"

_local = threading.local()
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    meeting_date TEXT DEFAULT '',
    background TEXT DEFAULT '',
    audio_path TEXT DEFAULT '',
    transcript_path TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS attendees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS minutes (
    meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    content TEXT DEFAULT '',
    error TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS meeting_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('audio', 'transcript')),
    position INTEGER NOT NULL DEFAULT 0,
    pair_key TEXT DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    generated INTEGER NOT NULL DEFAULT 0,
    processing_status TEXT NOT NULL DEFAULT 'ready',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(meeting_id, sha256, source_type, position)
);
CREATE INDEX IF NOT EXISTS idx_meeting_sources_order
    ON meeting_sources(meeting_id, position, id);
CREATE TABLE IF NOT EXISTS processing_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL DEFAULT 'organize',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT NOT NULL DEFAULT '',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    failure_fingerprint TEXT NOT NULL DEFAULT '',
    same_failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_queue ON processing_jobs(status, id);
CREATE TABLE IF NOT EXISTS processing_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES processing_jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_processing_job_events_job
    ON processing_job_events(job_id, id);
CREATE TABLE IF NOT EXISTS record_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    structured_json TEXT NOT NULL,
    source_fragments_json TEXT NOT NULL DEFAULT '{"items":[]}',
    markdown TEXT NOT NULL,
    docx_path TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    editor_kind TEXT NOT NULL DEFAULT 'model',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(meeting_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_record_revisions_latest
    ON record_revisions(meeting_id, revision DESC);
CREATE TABLE IF NOT EXISTS sync_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('upsert', 'delete')),
    revision INTEGER NOT NULL DEFAULT 0,
    meeting_date TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        _local.conn = conn
    return conn


def _add_column_if_missing(table: str, definition: str) -> None:
    column = definition.split()[0]
    columns = {row["name"] for row in get_conn().execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        get_conn().execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _migrate_legacy_sources() -> None:
    conn = get_conn()
    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key='legacy_sources_v1'"
    ).fetchone()
    if marker:
        return
    meetings = conn.execute(
        "SELECT id, audio_path, transcript_path FROM meetings ORDER BY id"
    ).fetchall()
    for meeting in meetings:
        position = 0
        for source_type, field in (("audio", "audio_path"), ("transcript", "transcript_path")):
            path = meeting[field] or ""
            if not path or not Path(path).is_file():
                continue
            text = ""
            if source_type == "transcript":
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            conn.execute(
                """INSERT OR IGNORE INTO meeting_sources
                   (meeting_id, source_type, position, pair_key, original_name,
                    stored_path, sha256, text_content, generated)
                   VALUES (?, ?, ?, 'legacy-1', ?, ?, ?, ?, 0)""",
                (meeting["id"], source_type, position, Path(path).name,
                 path, file_sha256(path), text),
            )
            position += 1
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('legacy_sources_v1','done')"
    )


_LEGACY_SECTION_MAP = [
    ("core-conclusion", "核心结论", "callout", "会议达成一致"),
    ("compilation-notes", "编制说明", "prose", ""),
    ("agenda-overview", "议题总览与总体判断", "table", "会议纪要"),
    ("consensus", "会议共识", "table", "会议达成一致"),
    ("requirements", "需求与约束", "table", ""),
    ("topic-details", "逐议题详细记录", "topic", "会议纪要"),
    ("open-items", "未决事项", "table", "会议未达成一致"),
    ("actions", "行动安排", "table", "会议待办"),
    ("risks", "风险与关注事项", "table", "观点冲突点"),
    ("pending-decisions", "待确认决策", "callout", "会议未达成一致"),
    ("closing", "结语", "prose", ""),
    ("recognition-review", "转写辨识与复核清单", "table", ""),
]


def _legacy_markdown_sections(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current:
                result[current] = "\n".join(lines).strip()
            current, lines = match.group(1).strip(), []
        elif current:
            lines.append(line)
    if current:
        result[current] = "\n".join(lines).strip()
    if not result and content.strip():
        result["会议纪要"] = content.strip()
    return result


def _migrate_legacy_minutes() -> None:
    """Wrap completed six-section results as immutable revision 1 records."""
    conn = get_conn()
    if conn.execute(
        "SELECT 1 FROM schema_meta WHERE key='legacy_minutes_v1'"
    ).fetchone():
        return
    rows = conn.execute(
        """SELECT m.*,mi.content,mi.updated_at minutes_updated_at
           FROM meetings m JOIN minutes mi ON mi.meeting_id=m.id
           WHERE mi.status='done' AND TRIM(mi.content)<>''
           AND NOT EXISTS (SELECT 1 FROM record_revisions r WHERE r.meeting_id=m.id)
           ORDER BY m.id"""
    ).fetchall()
    for row in rows:
        legacy = _legacy_markdown_sections(str(row["content"]))
        attendees = [dict(item) for item in conn.execute(
            "SELECT name,role FROM attendees WHERE meeting_id=? ORDER BY id", (row["id"],)
        )]
        compilation_note = (
            "本记录由升级前六段式会议纪要无损迁入；原结果未包含稳定片段编号，"
            "因此不补造证据引用。后续重新整理时将生成可追溯的详细记录。"
        )
        sections = []
        for section_id, title, kind, legacy_title in _LEGACY_SECTION_MAP:
            content = legacy.get(legacy_title, "") if legacy_title else ""
            if section_id == "compilation-notes":
                content = compilation_note
            elif section_id == "closing" and not content:
                content = "以上内容保持升级前纪要原意；需更高颗粒度时可重新整理。"
            sections.append({
                "id": section_id,
                "title": title,
                "kind": kind,
                "content": content or "升级前纪要未形成明确内容",
                "source_refs": [],
            })
        structured = {
            "schema_version": 1,
            "title": row["title"],
            "subtitle": "详细会议记录（历史迁移）",
            "meeting_meta": {
                "title": row["title"], "meeting_date": row["meeting_date"],
                "background": row["background"], "attendees": attendees,
            },
            "core_conclusion": sections[0]["content"],
            "sections": sections,
            "recognition_notes": ["历史结果未保留稳定片段引用，未进行反向猜测。"],
            "provenance": {
                "model_id": "legacy-unverified",
                "prompt_version": "legacy-six-section",
                "migration": "legacy_minutes_v1",
            },
            "meeting_id": int(row["id"]),
            "revision": 1,
        }
        raw = _canonical_json(structured)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        input_digest = hashlib.sha256(
            (str(row["content"]) + str(row["meeting_date"])).encode("utf-8")
        ).hexdigest()
        conn.execute(
            """INSERT INTO record_revisions
               (meeting_id,revision,structured_json,markdown,input_hash,content_hash,
                model_id,prompt_version,editor_kind,created_at)
               VALUES (?,1,?,?,?,?,?,'legacy-six-section','legacy-migration',?)""",
            (row["id"], raw, row["content"], input_digest, digest,
             "legacy-unverified", row["minutes_updated_at"]),
        )
        payload = {
            "meeting_id": int(row["id"]), "revision": 1,
            "meeting": {
                "title": row["title"], "meeting_date": row["meeting_date"],
                "background": row["background"],
            },
            "structured": structured, "content_hash": digest,
            "model_id": "legacy-unverified", "prompt_version": "legacy-six-section",
            "editor_kind": "legacy-migration",
        }
        conn.execute(
            """INSERT INTO sync_events
               (meeting_id,event_type,revision,meeting_date,payload_json,content_hash)
               VALUES (?,'upsert',1,?,?,?)""",
            (row["id"], row["meeting_date"], _canonical_json(payload), digest),
        )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('legacy_minutes_v1','done')"
    )


def _recover_legacy_processing_state() -> None:
    """Do not leave pre-queue background threads permanently 'processing'."""
    get_conn().execute(
        """UPDATE minutes SET status='pending',stage='',
           error='应用升级期间的旧整理任务已中断，请重新开始整理',
           updated_at=datetime('now','localtime')
           WHERE status='processing' AND NOT EXISTS (
             SELECT 1 FROM processing_jobs j WHERE j.meeting_id=minutes.meeting_id
             AND j.status IN ('queued','waiting','running')
           )"""
    )


def init_db() -> None:
    conn = get_conn()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"会议数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，拒绝降级写入"
        )
    conn.executescript(SCHEMA)
    _add_column_if_missing("meetings", "updated_at TEXT DEFAULT ''")
    _add_column_if_missing("minutes", "stage TEXT DEFAULT ''")
    _add_column_if_missing(
        "record_revisions", "source_fragments_json TEXT NOT NULL DEFAULT '{\"items\":[]}'"
    )
    _add_column_if_missing("processing_jobs", "heartbeat_at TEXT")
    _add_column_if_missing("processing_jobs", "last_error_code TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("processing_jobs", "failure_fingerprint TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("processing_jobs", "same_failure_count INTEGER NOT NULL DEFAULT 0")
    _migrate_legacy_sources()
    _migrate_legacy_minutes()
    _recover_legacy_processing_state()
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def create_meeting(title: str, meeting_date: str, background: str,
                   attendees: list[dict[str, str]]) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO meetings(title,meeting_date,background) VALUES (?,?,?)",
        (title, meeting_date, background),
    )
    meeting_id = int(cur.lastrowid)
    for attendee in attendees:
        name = (attendee.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT INTO attendees(meeting_id,name,role) VALUES (?,?,?)",
                (meeting_id, name, (attendee.get("role") or "").strip()),
            )
    conn.execute("INSERT INTO minutes(meeting_id,status) VALUES (?,'pending')", (meeting_id,))
    conn.commit()
    return meeting_id


def update_meeting(meeting_id: int, *, title: str, meeting_date: str,
                   background: str) -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE meetings SET title=?, meeting_date=?, background=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (title, meeting_date, background, meeting_id),
    )
    conn.commit()


def list_meetings() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        """SELECT m.id,m.title,m.meeting_date,m.created_at,
                  SUM(CASE WHEN s.source_type='audio' THEN 1 ELSE 0 END) audio_count,
                  SUM(CASE WHEN s.source_type='transcript' THEN 1 ELSE 0 END) transcript_count,
                  COALESCE(mi.status,'pending') minutes_status,
                  COALESCE((SELECT MAX(revision) FROM record_revisions r
                            WHERE r.meeting_id=m.id),0) current_revision
           FROM meetings m
           LEFT JOIN meeting_sources s ON s.meeting_id=m.id
           LEFT JOIN minutes mi ON mi.meeting_id=m.id
           GROUP BY m.id ORDER BY m.id DESC"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["has_audio"] = bool(item["audio_count"])
        item["has_transcript"] = bool(item["transcript_count"])
        result.append(item)
    return result


def get_meeting(meeting_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None:
        return None
    meeting = dict(row)
    meeting["attendees"] = [dict(item) for item in conn.execute(
        "SELECT name,role FROM attendees WHERE meeting_id=? ORDER BY id", (meeting_id,)
    )]
    meeting["sources"] = list_sources(meeting_id)
    meeting["job"] = get_latest_job(meeting_id)
    latest = get_record(meeting_id)
    meeting["current_revision"] = latest["revision"] if latest else 0
    return meeting


def _next_position(meeting_id: int) -> int:
    row = get_conn().execute(
        "SELECT COALESCE(MAX(position),-1)+1 next_position FROM meeting_sources WHERE meeting_id=?",
        (meeting_id,),
    ).fetchone()
    return int(row["next_position"])


def add_source(meeting_id: int, *, source_type: str, original_name: str,
               stored_path: str, sha256: str, text_content: str = "",
               pair_key: str = "", generated: bool = False) -> dict[str, Any]:
    if source_type not in {"audio", "transcript"}:
        raise ValueError("invalid source type")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO meeting_sources
           (meeting_id,source_type,position,pair_key,original_name,stored_path,
            sha256,text_content,generated) VALUES (?,?,?,?,?,?,?,?,?)""",
        (meeting_id, source_type, _next_position(meeting_id), pair_key.strip(),
         original_name, stored_path, sha256, text_content, int(generated)),
    )
    legacy_field = "audio_path" if source_type == "audio" else "transcript_path"
    existing = conn.execute(
        f"SELECT {legacy_field} value FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if existing and not existing["value"]:
        conn.execute(f"UPDATE meetings SET {legacy_field}=? WHERE id=?", (stored_path, meeting_id))
    conn.commit()
    return get_source(meeting_id, int(cur.lastrowid)) or {}


def get_source(meeting_id: int, source_id: int) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM meeting_sources WHERE meeting_id=? AND id=?", (meeting_id, source_id)
    ).fetchone()
    return dict(row) if row else None


def list_sources(meeting_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in get_conn().execute(
        "SELECT * FROM meeting_sources WHERE meeting_id=? ORDER BY position,id", (meeting_id,)
    )]


def find_duplicate_source(meeting_id: int, *, source_type: str, sha256: str,
                          pair_key: str = "", generated: bool = False) -> dict[str, Any] | None:
    row = get_conn().execute(
        """SELECT * FROM meeting_sources WHERE meeting_id=? AND source_type=?
           AND sha256=? AND TRIM(pair_key)=? AND generated=?
           ORDER BY position,id LIMIT 1""",
        (meeting_id, source_type, sha256, pair_key.strip(), int(generated)),
    ).fetchone()
    return dict(row) if row else None


def reorder_sources(meeting_id: int, source_ids: list[int]) -> None:
    conn = get_conn()
    existing = [row["id"] for row in conn.execute(
        "SELECT id FROM meeting_sources WHERE meeting_id=? ORDER BY position,id", (meeting_id,)
    )]
    if sorted(existing) != sorted(source_ids) or len(existing) != len(source_ids):
        raise ValueError("source order must contain every source exactly once")
    # Use a temporary collision-free range so byte-identical sources can swap.
    for offset, source_id in enumerate(source_ids, 1):
        conn.execute(
            "UPDATE meeting_sources SET position=? WHERE meeting_id=? AND id=?",
            (-offset, meeting_id, source_id),
        )
    for position, source_id in enumerate(source_ids):
        conn.execute(
            """UPDATE meeting_sources SET position=?,updated_at=datetime('now','localtime')
               WHERE meeting_id=? AND id=?""",
            (position, meeting_id, source_id),
        )
    conn.commit()


def set_source_pair(meeting_id: int, source_id: int, pair_key: str) -> None:
    conn = get_conn()
    cur = conn.execute(
        """UPDATE meeting_sources SET pair_key=?,updated_at=datetime('now','localtime')
           WHERE meeting_id=? AND id=?""",
        (pair_key.strip(), meeting_id, source_id),
    )
    if cur.rowcount != 1:
        raise KeyError(source_id)
    conn.commit()


def set_source_processing(source_id: int, status: str, error: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE meeting_sources SET processing_status=?,error=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (status, error, source_id),
    )
    conn.commit()


def delete_source(meeting_id: int, source_id: int) -> str:
    conn = get_conn()
    source = get_source(meeting_id, source_id)
    if source is None:
        raise KeyError(source_id)
    conn.execute("DELETE FROM meeting_sources WHERE meeting_id=? AND id=?", (meeting_id, source_id))
    legacy_field = "audio_path" if source["source_type"] == "audio" else "transcript_path"
    legacy = conn.execute(
        f"SELECT {legacy_field} value FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if legacy and legacy["value"] == source["stored_path"]:
        replacement = conn.execute(
            """SELECT stored_path FROM meeting_sources WHERE meeting_id=?
               AND source_type=? ORDER BY position,id LIMIT 1""",
            (meeting_id, source["source_type"]),
        ).fetchone()
        conn.execute(
            f"UPDATE meetings SET {legacy_field}=? WHERE id=?",
            (replacement["stored_path"] if replacement else "", meeting_id),
        )
    conn.commit()
    return source["stored_path"]


def source_text(source: dict[str, Any]) -> str:
    if source.get("text_content"):
        return str(source["text_content"])
    path = source.get("stored_path") or ""
    return Path(path).read_text(encoding="utf-8", errors="replace") if path and Path(path).is_file() else ""


def set_minutes_status(meeting_id: int, status: str, content: str = "",
                       error: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO minutes(meeting_id,status,content,error,updated_at)
           VALUES (?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(meeting_id) DO UPDATE SET status=excluded.status,
           content=excluded.content,error=excluded.error,updated_at=excluded.updated_at""",
        (meeting_id, status, content, error),
    )
    conn.commit()


def set_stage(meeting_id: int, stage: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE minutes SET stage=?,updated_at=datetime('now','localtime') WHERE meeting_id=?",
        (stage, meeting_id),
    )
    conn.commit()


def get_minutes(meeting_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM minutes WHERE meeting_id=?", (meeting_id,)).fetchone()
    return dict(row) if row else None


def read_transcript(meeting: dict[str, Any]) -> str:
    transcripts = [source_text(source) for source in meeting.get("sources", [])
                   if source["source_type"] == "transcript"]
    if transcripts:
        return "\n\n".join(part for part in transcripts if part.strip())
    path = meeting.get("transcript_path") or ""
    return Path(path).read_text(encoding="utf-8", errors="replace") if path else ""


def enqueue_job(meeting_id: int, input_hash: str) -> dict[str, Any]:
    conn = get_conn()
    active = conn.execute(
        """SELECT * FROM processing_jobs WHERE meeting_id=?
           AND status IN ('queued','waiting','running') ORDER BY id DESC LIMIT 1""",
        (meeting_id,),
    ).fetchone()
    if active:
        return dict(active)
    failed_rows = conn.execute(
        """SELECT * FROM processing_jobs WHERE meeting_id=? AND input_hash=?
           AND status='failed' ORDER BY id DESC""",
        (meeting_id, input_hash),
    ).fetchall()
    if not failed_rows:
        # Reuse the newest recoverable job even when canonical source
        # de-duplication changed the input hash.  The processor reconciles the
        # saved fragment prefix before it uses any checkpoint content.
        failed_rows = conn.execute(
            """SELECT * FROM processing_jobs WHERE meeting_id=? AND status='failed'
               ORDER BY id DESC""",
            (meeting_id,),
        ).fetchall()
    for failed_row in failed_rows:
        try:
            checkpoint = json.loads(failed_row["checkpoint_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not checkpoint.get("extracted"):
            continue
        conn.execute(
            """UPDATE processing_jobs SET status='queued',stage='从失败断点恢复',
               progress=1,error='',finished_at=NULL,last_error_code='',
               failure_fingerprint='',same_failure_count=0,
               heartbeat_at=datetime('now','localtime'),
               updated_at=datetime('now','localtime')
               WHERE id=?""",
            (failed_row["id"],),
        )
        record_job_event(
            int(failed_row["id"]), status="queued", stage="从失败断点恢复",
            message="复用已保存的分块断点",
        )
        conn.commit()
        set_minutes_status(meeting_id, "processing")
        return get_job(int(failed_row["id"])) or {}
    cur = conn.execute(
        "INSERT INTO processing_jobs(meeting_id,status,stage,input_hash) VALUES (?,'queued','等待处理',?)",
        (meeting_id, input_hash),
    )
    conn.commit()
    set_minutes_status(meeting_id, "processing")
    return get_job(int(cur.lastrowid)) or {}


def recover_jobs() -> int:
    conn = get_conn()
    cur = conn.execute(
        """UPDATE processing_jobs SET status='queued',stage='应用重启后恢复排队',
           heartbeat_at=datetime('now','localtime'),
           updated_at=datetime('now','localtime') WHERE status IN ('running','waiting')"""
    )
    conn.commit()
    return cur.rowcount


def reset_job_input(job_id: int, input_hash: str, *, status: str = "running",
                    checkpoint: dict[str, Any] | None = None) -> None:
    """Reconcile stage checkpoints when the effective ordered input changed."""
    conn = get_conn()
    conn.execute(
        """UPDATE processing_jobs SET input_hash=?,status=?,checkpoint_json=?,
           stage='输入已更新，重新开始',progress=1,
           last_error_code='',failure_fingerprint='',same_failure_count=0,
           heartbeat_at=datetime('now','localtime'),
           updated_at=datetime('now','localtime') WHERE id=?""",
        (input_hash, status, json.dumps(checkpoint or {}, ensure_ascii=False, sort_keys=True), job_id),
    )
    conn.commit()


def claim_next_job() -> dict[str, Any] | None:
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM processing_jobs WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute(
        """UPDATE processing_jobs SET status='running',stage='准备输入',progress=1,
           attempts=attempts+1,started_at=COALESCE(started_at,datetime('now','localtime')),
           error='',last_error_code='',heartbeat_at=datetime('now','localtime'),
           updated_at=datetime('now','localtime') WHERE id=?""",
        (row["id"],),
    )
    conn.commit()
    record_job_event(int(row["id"]), status="running", stage="准备输入")
    return get_job(int(row["id"]))


def get_job(job_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM processing_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_latest_job(meeting_id: int) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM processing_jobs WHERE meeting_id=? ORDER BY id DESC LIMIT 1", (meeting_id,)
    ).fetchone()
    return dict(row) if row else None


def update_job(job_id: int, *, status: str | None = None, stage: str | None = None,
               progress: int | None = None, checkpoint: dict[str, Any] | None = None,
               error: str | None = None, last_error_code: str | None = None) -> None:
    fields = [
        "updated_at=datetime('now','localtime')",
        "heartbeat_at=datetime('now','localtime')",
    ]
    values: list[Any] = []
    for column, value in (("status", status), ("stage", stage),
                          ("progress", progress), ("error", error)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    if checkpoint is not None:
        fields.append("checkpoint_json=?")
        values.append(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True))
    if last_error_code is not None:
        fields.append("last_error_code=?")
        values.append(last_error_code)
    values.append(job_id)
    conn = get_conn()
    conn.execute(f"UPDATE processing_jobs SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()


def record_job_event(job_id: int, *, status: str = "", stage: str = "",
                     error_code: str = "", message: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO processing_job_events(job_id,status,stage,error_code,message)
           VALUES (?,?,?,?,?)""",
        (job_id, status, stage, error_code, message[:1000]),
    )
    conn.commit()


def record_deterministic_failure(job_id: int, *, fingerprint: str,
                                 error_code: str, message: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT failure_fingerprint,same_failure_count FROM processing_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return 0
    count = int(row["same_failure_count"] or 0) + 1 if row["failure_fingerprint"] == fingerprint else 1
    conn.execute(
        """UPDATE processing_jobs SET failure_fingerprint=?,same_failure_count=?,
           last_error_code=?,error=?,heartbeat_at=datetime('now','localtime'),
           updated_at=datetime('now','localtime') WHERE id=?""",
        (fingerprint, count, error_code, message, job_id),
    )
    conn.commit()
    record_job_event(
        job_id, status="waiting", stage="确定性输出校验失败",
        error_code=error_code, message=message,
    )
    return count


def finish_job(job_id: int, *, error: str = "", error_code: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE processing_jobs SET status=?,stage=?,progress=?,error=?,
           last_error_code=?,heartbeat_at=datetime('now','localtime'),
           finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
           WHERE id=?""",
        ("failed" if error else "done", "处理失败" if error else "处理完成",
         0 if error else 100, error, error_code, job_id),
    )
    conn.commit()
    record_job_event(
        job_id, status="failed" if error else "done",
        stage="处理失败" if error else "处理完成",
        error_code=error_code, message=error,
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_revision(meeting_id: int, structured: dict[str, Any], markdown: str,
                  *, input_hash: str, model_id: str, prompt_version: str,
                  editor_kind: str, base_revision: int | None,
                  docx_path: str = "",
                  source_fragments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    current = int(conn.execute(
        "SELECT COALESCE(MAX(revision),0) revision FROM record_revisions WHERE meeting_id=?",
        (meeting_id,),
    ).fetchone()["revision"])
    if base_revision is not None and base_revision != current:
        conn.rollback()
        raise ValueError(f"revision conflict: current revision is {current}")
    revision = current + 1
    if source_fragments is None and current:
        previous_fragments = conn.execute(
            """SELECT source_fragments_json FROM record_revisions
               WHERE meeting_id=? AND revision=?""",
            (meeting_id, current),
        ).fetchone()
        decoded_fragments = json.loads(previous_fragments["source_fragments_json"])
        source_fragments = (
            decoded_fragments
            if isinstance(decoded_fragments, list)
            else decoded_fragments.get("items") or []
        )
    source_fragments = list(source_fragments or [])
    structured = dict(structured)
    structured["meeting_id"] = meeting_id
    structured["revision"] = revision
    raw = _canonical_json(structured)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO record_revisions
           (meeting_id,revision,structured_json,source_fragments_json,markdown,docx_path,input_hash,
            content_hash,model_id,prompt_version,editor_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (meeting_id, revision, raw, _canonical_json({"items": source_fragments}),
         markdown, docx_path, input_hash, digest,
         model_id, prompt_version, editor_kind),
    )
    meeting = conn.execute(
        "SELECT title,meeting_date,background FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    payload = {
        "meeting_id": meeting_id, "revision": revision, "meeting": dict(meeting),
        "structured": structured, "content_hash": digest, "model_id": model_id,
        "prompt_version": prompt_version, "editor_kind": editor_kind,
    }
    conn.execute(
        """INSERT INTO sync_events
           (meeting_id,event_type,revision,meeting_date,payload_json,content_hash)
           VALUES (?,'upsert',?,?,?,?)""",
        (meeting_id, revision, meeting["meeting_date"], _canonical_json(payload), digest),
    )
    conn.commit()
    return get_record(meeting_id, revision) or {}


def set_revision_docx(meeting_id: int, revision: int, path: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE record_revisions SET docx_path=? WHERE meeting_id=? AND revision=?",
        (path, meeting_id, revision),
    )
    conn.commit()


def get_record(meeting_id: int, revision: int | None = None) -> dict[str, Any] | None:
    if revision is None:
        row = get_conn().execute(
            "SELECT * FROM record_revisions WHERE meeting_id=? ORDER BY revision DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
    else:
        row = get_conn().execute(
            "SELECT * FROM record_revisions WHERE meeting_id=? AND revision=?",
            (meeting_id, revision),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["structured"] = json.loads(result.pop("structured_json"))
    fragments = json.loads(result.pop("source_fragments_json") or "{}")
    result["source_fragments"] = list(
        fragments if isinstance(fragments, list) else fragments.get("items") or []
    )
    return result


def list_revisions(meeting_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in get_conn().execute(
        """SELECT revision,content_hash,input_hash,model_id,prompt_version,
                  editor_kind,created_at FROM record_revisions
           WHERE meeting_id=? ORDER BY revision DESC""",
        (meeting_id,),
    )]


def export_events(after: int, limit: int = 200) -> dict[str, Any]:
    rows = get_conn().execute(
        "SELECT * FROM sync_events WHERE seq>? ORDER BY seq LIMIT ?", (after, min(limit, 1000))
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        events.append(event)
    return {"after": after, "cursor": events[-1]["seq"] if events else after, "events": events}


def delete_meeting(meeting_id: int) -> list[str]:
    conn = get_conn()
    meeting = conn.execute(
        "SELECT title,meeting_date FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        return []
    files = [row[0] for row in conn.execute(
        "SELECT stored_path FROM meeting_sources WHERE meeting_id=? AND stored_path!=''", (meeting_id,)
    )]
    files.extend(row[0] for row in conn.execute(
        "SELECT docx_path FROM record_revisions WHERE meeting_id=? AND docx_path!=''", (meeting_id,)
    ))
    legacy = conn.execute(
        "SELECT audio_path,transcript_path FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    files.extend(path for path in legacy if path)
    tombstone = {
        "meeting_id": meeting_id, "meeting_date": meeting["meeting_date"],
        "title": meeting["title"], "deleted_at": datetime.now().isoformat(timespec="seconds"),
    }
    raw = _canonical_json(tombstone)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    latest = conn.execute(
        "SELECT COALESCE(MAX(revision),0) FROM record_revisions WHERE meeting_id=?", (meeting_id,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO sync_events
           (meeting_id,event_type,revision,meeting_date,payload_json,content_hash)
           VALUES (?,'delete',?,?,?,?)""",
        (meeting_id, latest, meeting["meeting_date"], raw, digest),
    )
    conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
    conn.commit()
    return list(dict.fromkeys(files))
