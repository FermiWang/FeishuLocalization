from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .meeting_records_database import MeetingRecordsDatabase

REMOTE_EXPORT_DIRECTORY = "/Users/apple/meeting-minutes"


class MeetingRecordsSyncError(RuntimeError):
    pass


def _safe_ssh_atom(value: str, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", value):
        raise ValueError(f"invalid {name}")
    return value


class SSHMeetingRecordsExporter:
    def __init__(self, *, host: str = "192.168.100.179", user: str = "apple",
                 identity_file: str | None = None, timeout: int = 45) -> None:
        self.host = _safe_ssh_atom(host, "host")
        self.user = _safe_ssh_atom(user, "user")
        self.identity_file = identity_file
        self.timeout = timeout

    def fetch(self, after: int, limit: int) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid meeting export cursor or limit")
        argv = [
            "ssh", "-F", "/dev/null",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10",
            "-o", "IdentityAgent=none",
            "-o", "IdentitiesOnly=yes",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PasswordAuthentication=no",
            "-o", "PreferredAuthentications=publickey",
        ]
        if self.identity_file:
            candidate = Path(self.identity_file).expanduser()
            if not candidate.is_absolute() or not candidate.is_file():
                raise ValueError("meeting sync identity file must be an existing absolute path")
            candidate = candidate.resolve()
            argv.extend(["-i", str(candidate)])
        argv.extend([
            "--",
            f"{self.user}@{self.host}",
            (
                f"cd {REMOTE_EXPORT_DIRECTORY} && "
                f".venv/bin/python3 -m app.export_events --after {after} --limit {limit}"
            ),
        ])
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "SSH export failed").strip().splitlines()[-1]
            raise MeetingRecordsSyncError(detail[:500])
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise MeetingRecordsSyncError("meeting export returned invalid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            raise MeetingRecordsSyncError("meeting export JSON shape is invalid")
        return value


def sync_meeting_records(
    database: MeetingRecordsDatabase,
    insights_database: Any | None,
    *,
    trigger: str,
    exporter: SSHMeetingRecordsExporter | Any,
    limit: int = 200,
    max_pages: int = 50,
) -> dict[str, Any]:
    run_id = database.start_sync(trigger)
    cursor = database.cursor()
    applied = 0
    try:
        report_dates: set[str] = set()
        if insights_database is not None:
            with insights_database.connection() as con:
                report_dates = {
                    str(row[0]) for row in con.execute(
                        "SELECT DISTINCT report_date FROM analysis_runs WHERE status='success' AND report_date<>''"
                    )
                }
        for _ in range(max_pages):
            page = exporter.fetch(cursor, limit)
            events = list(page.get("events") or [])
            page_cursor = int(page.get("cursor") or cursor)
            if page_cursor < cursor:
                raise MeetingRecordsSyncError("meeting export cursor moved backwards")
            if events:
                try:
                    sequences = [int(event["seq"]) for event in events]
                except (KeyError, TypeError, ValueError) as exc:
                    raise MeetingRecordsSyncError("meeting export event sequence is invalid") from exc
                if (
                    sequences != sorted(set(sequences))
                    or sequences[0] <= cursor
                    or sequences != list(range(cursor + 1, cursor + 1 + len(sequences)))
                    or page_cursor != sequences[-1]
                ):
                    raise MeetingRecordsSyncError("meeting export event cursor is inconsistent")
            elif page_cursor != cursor:
                raise MeetingRecordsSyncError("meeting export advanced an empty page")
            applied += database.apply_events(events, report_dates=report_dates)
            cursor = page_cursor
            if len(events) < limit:
                break
        else:
            raise MeetingRecordsSyncError("meeting export exceeded maximum pages")
        database.finish_sync(run_id, status="success", cursor=cursor, events_applied=applied)
        return {"status": "success", "cursor": cursor, "events_applied": applied}
    except Exception as exc:
        database.finish_sync(
            run_id, status="error", cursor=cursor, events_applied=applied,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
