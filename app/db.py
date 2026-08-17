"""SQLite 存储层：会议、参会人、纪要结果。"""
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "meetings.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    meeting_date TEXT DEFAULT '',
    background TEXT DEFAULT '',
    audio_path TEXT DEFAULT '',
    transcript_path TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
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
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    try:  # 老库迁移：补 stage 列
        conn.execute("ALTER TABLE minutes ADD COLUMN stage TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def create_meeting(title: str, meeting_date: str, background: str,
                   attendees: list[dict]) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO meetings (title, meeting_date, background) VALUES (?, ?, ?)",
        (title, meeting_date, background),
    )
    meeting_id = cur.lastrowid
    for a in attendees:
        name = (a.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT INTO attendees (meeting_id, name, role) VALUES (?, ?, ?)",
                (meeting_id, name, (a.get("role") or "").strip()),
            )
    conn.execute(
        "INSERT INTO minutes (meeting_id, status) VALUES (?, 'pending')",
        (meeting_id,),
    )
    conn.commit()
    return meeting_id


def list_meetings() -> list[dict]:
    rows = get_conn().execute(
        """SELECT m.id, m.title, m.meeting_date, m.created_at,
                  (m.audio_path != '') AS has_audio,
                  (m.transcript_path != '') AS has_transcript,
                  COALESCE(mi.status, 'pending') AS minutes_status
           FROM meetings m LEFT JOIN minutes mi ON mi.meeting_id = m.id
           ORDER BY m.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_meeting(meeting_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        return None
    meeting = dict(row)
    meeting["attendees"] = [
        dict(r) for r in conn.execute(
            "SELECT name, role FROM attendees WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
    ]
    return meeting


def update_meeting_file(meeting_id: int, field: str, path: str) -> None:
    assert field in ("audio_path", "transcript_path")
    conn = get_conn()
    conn.execute(f"UPDATE meetings SET {field} = ? WHERE id = ?", (path, meeting_id))
    conn.commit()


def set_minutes_status(meeting_id: int, status: str,
                       content: str = "", error: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO minutes (meeting_id, status, content, error, updated_at)
           VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
           ON CONFLICT(meeting_id) DO UPDATE SET
               status = excluded.status, content = excluded.content,
               error = excluded.error, updated_at = excluded.updated_at""",
        (meeting_id, status, content, error),
    )
    conn.commit()


def set_stage(meeting_id: int, stage: str) -> None:
    """更新整理所处的阶段提示（如「声纹分离中」「模型整理中」）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE minutes SET stage = ?, updated_at = datetime('now', 'localtime') "
        "WHERE meeting_id = ?",
        (stage, meeting_id),
    )
    conn.commit()


def get_minutes(meeting_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM minutes WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    return dict(row) if row else None


def read_transcript(meeting: dict) -> str:
    path = meeting.get("transcript_path") or ""
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def delete_meeting(meeting_id: int) -> list[str]:
    """删除会议全部数据库记录，返回待清理的文件路径列表。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT audio_path, transcript_path FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return []
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    conn.commit()
    return [p for p in (row["audio_path"], row["transcript_path"]) if p]
