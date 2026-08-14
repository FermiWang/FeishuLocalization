from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


INSIGHTS_SCHEMA_VERSION = 1


_MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE analysis_runs (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    trigger TEXT NOT NULL DEFAULT 'manual',
    report_date TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    window_start INTEGER,
    window_end INTEGER,
    snapshot_at INTEGER,
    source_snapshot_hash TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    stats_json TEXT NOT NULL DEFAULT '{}',
    report_json TEXT,
    report_markdown TEXT,
    report_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running', 'success', 'partial', 'error', 'interrupted')),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    error TEXT,
    CHECK(window_start IS NULL OR window_end IS NULL OR window_start < window_end),
    CHECK(is_active = 0 OR status = 'success')
);

CREATE UNIQUE INDEX idx_analysis_runs_one_active
    ON analysis_runs(is_active) WHERE is_active = 1;
CREATE INDEX idx_analysis_runs_latest
    ON analysis_runs(started_at DESC, id DESC);

CREATE TABLE evidence_sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    container_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    occurred_at INTEGER,
    content_text TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    UNIQUE(source_kind, source_id, source_version)
);

CREATE INDEX idx_evidence_sources_lookup
    ON evidence_sources(source_kind, source_id, occurred_at DESC);

CREATE TABLE run_evidence (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    evidence_source_id INTEGER NOT NULL REFERENCES evidence_sources(id) ON DELETE RESTRICT,
    evidence_key TEXT NOT NULL,
    span_start INTEGER NOT NULL DEFAULT 0 CHECK(span_start >= 0),
    span_end INTEGER NOT NULL CHECK(span_end >= span_start),
    excerpt_text TEXT NOT NULL DEFAULT '',
    excerpt_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    UNIQUE(run_id, evidence_key)
);

CREATE INDEX idx_run_evidence_source
    ON run_evidence(evidence_source_id, run_id);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    task_key TEXT NOT NULL UNIQUE,
    dedupe_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    project_key TEXT NOT NULL DEFAULT '',
    owner_key TEXT NOT NULL DEFAULT '',
    due_at INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'waiting', 'blocked', 'scheduled', 'done', 'canceled', 'superseded')),
    status_source TEXT NOT NULL DEFAULT 'machine'
        CHECK(status_source IN ('machine', 'manual')),
    confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    closed_at INTEGER,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_tasks_status_due
    ON tasks(status, due_at, last_seen_at DESC);
CREATE INDEX idx_tasks_dedupe
    ON tasks(dedupe_key, project_key, owner_key);

CREATE TABLE task_events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    run_id INTEGER REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('extractor', 'system', 'human')),
    from_status TEXT,
    to_status TEXT,
    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
    occurred_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_task_events_task
    ON task_events(task_id, id);

CREATE TABLE task_observations (
    id INTEGER PRIMARY KEY,
    observation_key TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    run_id INTEGER REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    run_evidence_id INTEGER REFERENCES run_evidence(id) ON DELETE RESTRICT,
    observed_status TEXT,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
    observed_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_task_observations_task
    ON task_observations(task_id, observed_at DESC, id DESC);

CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY,
    opportunity_key TEXT NOT NULL UNIQUE,
    entity_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'dismissed', 'converted', 'expired')),
    status_source TEXT NOT NULL DEFAULT 'machine'
        CHECK(status_source IN ('machine', 'manual')),
    score REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    signal_count INTEGER NOT NULL DEFAULT 0 CHECK(signal_count >= 0),
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_opportunities_status_score
    ON opportunities(status, score DESC, last_seen_at DESC);

CREATE TABLE opportunity_signals (
    id INTEGER PRIMARY KEY,
    signal_key TEXT NOT NULL UNIQUE,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE RESTRICT,
    run_id INTEGER REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    run_evidence_id INTEGER REFERENCES run_evidence(id) ON DELETE RESTRICT,
    signal_kind TEXT NOT NULL DEFAULT 'mention',
    score REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    observed_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_opportunity_signals_opportunity
    ON opportunity_signals(opportunity_id, observed_at DESC, id DESC);

CREATE TABLE report_citations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    run_evidence_id INTEGER NOT NULL REFERENCES run_evidence(id) ON DELETE RESTRICT,
    citation_key TEXT NOT NULL,
    section TEXT NOT NULL DEFAULT '',
    claim_key TEXT NOT NULL DEFAULT '',
    claim_text TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    UNIQUE(run_id, citation_key)
);

CREATE INDEX idx_report_citations_run
    ON report_citations(run_id, section, ordinal, id);

CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events BEGIN
    SELECT RAISE(ABORT, 'task_events are append-only');
END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events BEGIN
    SELECT RAISE(ABORT, 'task_events are append-only');
END;
CREATE TRIGGER task_observations_no_update BEFORE UPDATE ON task_observations BEGIN
    SELECT RAISE(ABORT, 'task_observations are append-only');
END;
CREATE TRIGGER task_observations_no_delete BEFORE DELETE ON task_observations BEGIN
    SELECT RAISE(ABORT, 'task_observations are append-only');
END;
CREATE TRIGGER opportunity_signals_no_update BEFORE UPDATE ON opportunity_signals BEGIN
    SELECT RAISE(ABORT, 'opportunity_signals are append-only');
END;
CREATE TRIGGER opportunity_signals_no_delete BEFORE DELETE ON opportunity_signals BEGIN
    SELECT RAISE(ABORT, 'opportunity_signals are append-only');
END;
CREATE TRIGGER report_citations_no_update BEFORE UPDATE ON report_citations BEGIN
    SELECT RAISE(ABORT, 'report_citations are append-only');
END;
CREATE TRIGGER report_citations_no_delete BEFORE DELETE ON report_citations BEGIN
    SELECT RAISE(ABORT, 'report_citations are append-only');
END;
""",
}


_TASK_STATUSES = {
    "open",
    "waiting",
    "blocked",
    "scheduled",
    "done",
    "canceled",
    "superseded",
}
_FINAL_RUN_STATUSES = {"success", "partial", "error", "interrupted"}
_OPPORTUNITY_STATUSES = {"active", "dismissed", "converted", "expired"}
_JSON_COLUMNS = {
    "request_json": "request",
    "config_json": "config",
    "coverage_json": "coverage",
    "stats_json": "stats",
    "report_json": "report",
    "metadata_json": "metadata",
    "payload_json": "payload",
}


class InsightsDatabase:
    """Independent, auditable SQLite store for the insights analysis lane."""

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
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        con = self.connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        con = self.connect()
        try:
            self._local.transaction_connection = con
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            self._local.transaction_connection = None
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            current = int(con.execute("PRAGMA user_version").fetchone()[0])
            if current > INSIGHTS_SCHEMA_VERSION:
                raise RuntimeError(
                    f"洞察数据库版本 {current} 高于程序支持版本 {INSIGHTS_SCHEMA_VERSION}"
                )
            for version in range(current + 1, INSIGHTS_SCHEMA_VERSION + 1):
                script = _MIGRATIONS.get(version)
                if script is None:
                    raise RuntimeError(f"缺少洞察数据库迁移版本 {version}")
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
        with self.connection() as con:
            sqlite_status = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        if sqlite_status == "ok" and foreign_keys == 0:
            return "ok"
        return _json_value(
            {"sqlite": sqlite_status, "foreign_key_violations": foreign_keys}
        )

    def start_run(
        self, item: dict[str, Any] | None = None, **values: Any
    ) -> dict[str, Any]:
        data = {**(item or {}), **values}
        now = _optional_int(data.get("started_at")) or _now_ms()
        snapshot_at = _optional_int(data.get("snapshot_at"))
        trigger = str(data.get("trigger") or "manual")
        identity = {
            "report_date": str(data.get("report_date") or ""),
            "timezone": str(data.get("timezone") or "UTC"),
            "window_start": _optional_int(data.get("window_start")),
            "window_end": _optional_int(data.get("window_end")),
            "source_snapshot_hash": str(data.get("source_snapshot_hash") or ""),
            "prompt_version": str(data.get("prompt_version") or ""),
            "model_id": str(data.get("model_id", data.get("model")) or ""),
            "config": _mapping(data.get("config", data.get("config_json"))),
            "coverage": _mapping(data.get("coverage", data.get("coverage_json"))),
        }
        request_json = _json_value(identity)
        run_key = str(data.get("run_key") or _sha256_text(request_json)).strip()
        if not run_key:
            raise ValueError("run_key 不能为空")
        with self.transaction() as con:
            existing = con.execute(
                "SELECT * FROM analysis_runs WHERE run_key=?", (run_key,)
            ).fetchone()
            if existing is not None:
                if str(existing["request_json"]) != request_json:
                    raise ValueError("run_key 已被不同的分析请求占用")
                return _decode_row(existing)
            cursor = con.execute(
                """
                INSERT INTO analysis_runs(
                    run_key, trigger, report_date, timezone, window_start, window_end,
                    snapshot_at, source_snapshot_hash, prompt_version, model_id,
                    request_json, config_json, coverage_json, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    trigger,
                    identity["report_date"],
                    identity["timezone"],
                    identity["window_start"],
                    identity["window_end"],
                    snapshot_at,
                    identity["source_snapshot_hash"],
                    identity["prompt_version"],
                    identity["model_id"],
                    request_json,
                    _json_value(identity["config"]),
                    _json_value(identity["coverage"]),
                    now,
                ),
            )
            row = con.execute(
                "SELECT * FROM analysis_runs WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return _decode_row(row)

    def find_reusable_run(
        self, run: int | str | dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the newest successful run with the same immutable request identity."""
        with self.connection() as con:
            if isinstance(run, dict):
                run_id = _optional_int(run.get("id"))
                run_key = str(run.get("run_key") or "")
            elif isinstance(run, int):
                run_id = run
                run_key = ""
            else:
                run_id = None
                run_key = str(run)
            if run_id is not None:
                source = con.execute(
                    "SELECT request_json FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
            else:
                source = con.execute(
                    "SELECT request_json FROM analysis_runs WHERE run_key=?", (run_key,)
                ).fetchone()
            if source is None:
                return None
            row = con.execute(
                """
                SELECT * FROM analysis_runs
                WHERE request_json=? AND status='success'
                ORDER BY finished_at DESC, id DESC LIMIT 1
                """,
                (str(source["request_json"]),),
            ).fetchone()
            return self._run_with_citations(con, row) if row else None

    def add_evidence(
        self,
        run: int | dict[str, Any],
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(run, dict):
            data = dict(run)
            run_id = _required_int(
                data.pop(
                    "run_id", data.pop("analysis_run_id", data.pop("id", None))
                ),
                "run_id",
            )
            if item:
                data.update(item)
        else:
            run_id = int(run)
            data = dict(item or {})

        source_kind = _required_text(data.get("source_kind"), "source_kind")
        provider_source_id = _required_text(
            data.get("source_id", data.get("provider_source_id")), "source_id"
        )
        content_text = str(
            data.get("content_text", data.get("content", data.get("text", ""))) or ""
        )
        content_sha256 = str(data.get("content_sha256") or _sha256_text(content_text))
        source_version = str(data.get("source_version") or content_sha256)
        source_key = str(
            data.get("source_key")
            or f"{source_kind}:{provider_source_id}:{source_version}"
        )
        excerpt_text = str(data.get("excerpt_text", data.get("excerpt", content_text)) or "")
        span_start = _optional_int(data.get("span_start")) or 0
        span_end = _optional_int(data.get("span_end"))
        if span_end is None:
            span_end = span_start + len(excerpt_text)
        if span_start < 0 or span_end < span_start:
            raise ValueError("证据范围无效")
        excerpt_sha256 = str(data.get("excerpt_sha256") or _sha256_text(excerpt_text))
        evidence_key = str(
            data.get("evidence_key")
            or _stable_key(
                "evidence", source_key, span_start, span_end, excerpt_sha256
            )
        )
        source_metadata_json = _json_value(
            _mapping(data.get("source_metadata", data.get("metadata")))
        )
        evidence_metadata_json = _json_value(
            _mapping(data.get("evidence_metadata", data.get("metadata")))
        )
        now = _optional_int(data.get("created_at")) or _now_ms()

        with self.transaction() as con:
            run_row = con.execute(
                "SELECT status FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise ValueError(f"分析任务不存在: {run_id}")
            existing_link = con.execute(
                "SELECT * FROM run_evidence WHERE run_id=? AND evidence_key=?",
                (run_id, evidence_key),
            ).fetchone()
            if existing_link is not None:
                result = self._get_evidence_with_connection(con, int(existing_link["id"]))
                if not _same_evidence(
                    result,
                    source_kind=source_kind,
                    source_id=provider_source_id,
                    source_version=source_version,
                    content_sha256=content_sha256,
                    span_start=span_start,
                    span_end=span_end,
                    excerpt_sha256=excerpt_sha256,
                ):
                    raise ValueError("evidence_key 已被不同的证据占用")
                return result
            if str(run_row["status"]) != "running":
                raise RuntimeError("已结束的分析任务不能添加新证据")

            source = con.execute(
                "SELECT * FROM evidence_sources WHERE source_key=?", (source_key,)
            ).fetchone()
            if source is None:
                duplicate = con.execute(
                    """
                    SELECT * FROM evidence_sources
                    WHERE source_kind=? AND source_id=? AND source_version=?
                    """,
                    (source_kind, provider_source_id, source_version),
                ).fetchone()
                source = duplicate
            if source is None:
                cursor = con.execute(
                    """
                    INSERT INTO evidence_sources(
                        source_key, source_kind, source_id, source_version,
                        container_id, title, author, occurred_at, content_text,
                        content_sha256, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        source_kind,
                        provider_source_id,
                        source_version,
                        str(data.get("container_id") or ""),
                        str(data.get("title") or ""),
                        str(data.get("author") or ""),
                        _optional_int(data.get("occurred_at")),
                        content_text,
                        content_sha256,
                        source_metadata_json,
                        now,
                    ),
                )
                source = con.execute(
                    "SELECT * FROM evidence_sources WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            elif (
                str(source["source_kind"]) != source_kind
                or str(source["source_id"]) != provider_source_id
                or str(source["source_version"]) != source_version
                or str(source["content_sha256"]) != content_sha256
                or str(source["content_text"]) != content_text
            ):
                raise ValueError("source_key/source_version 已被不同的来源内容占用")

            cursor = con.execute(
                """
                INSERT INTO run_evidence(
                    run_id, evidence_source_id, evidence_key, span_start, span_end,
                    excerpt_text, excerpt_sha256, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(source["id"]),
                    evidence_key,
                    span_start,
                    span_end,
                    excerpt_text,
                    excerpt_sha256,
                    evidence_metadata_json,
                    now,
                ),
            )
            return self._get_evidence_with_connection(con, int(cursor.lastrowid))

    def finish_run(
        self,
        run: int | dict[str, Any],
        result: dict[str, Any] | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        if isinstance(run, dict):
            data = dict(run)
            run_id = _required_int(data.pop("run_id", data.pop("id", None)), "run_id")
            if result:
                data.update(result)
        else:
            run_id = int(run)
            data = dict(result or {})
        data.update(values)
        final_status = str(data.get("status") or "success")
        if final_status not in _FINAL_RUN_STATUSES:
            raise ValueError(f"无效的分析任务结束状态: {final_status}")
        finished_at = _optional_int(data.get("finished_at")) or _now_ms()
        coverage_json = _json_value(_mapping(data.get("coverage")))
        stats_json = _json_value(_mapping(data.get("stats")))
        report_value = data.get("report", data.get("report_json"))
        if report_value is None:
            report_value = {}
        if isinstance(report_value, str):
            report_value = _json_load_mapping(report_value)
        report_json = _json_value(_mapping(report_value))
        report_markdown = str(data.get("report_markdown", data.get("markdown", "")) or "")
        report_sha256 = _sha256_text(
            _json_value({"report": _mapping(report_value), "markdown": report_markdown})
        )
        error = None if data.get("error") is None else str(data.get("error"))
        citations = data.get("citations") or []
        if not isinstance(citations, list):
            raise ValueError("citations 必须是列表")
        activate_requested = bool(data.get("activate", True))

        with self.transaction() as con:
            current = con.execute(
                "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"分析任务不存在: {run_id}")
            if str(current["status"]) != "running":
                if _same_finished_run(
                    current,
                    status=final_status,
                    report_sha256=report_sha256,
                    error=error,
                ):
                    return self._run_with_citations(con, current)
                raise RuntimeError("分析任务已结束，不能用不同结果再次完成")

            if final_status != "success":
                if final_status == "partial" and (report_value != {} or report_markdown):
                    self._insert_report_citations(con, run_id, citations, finished_at)
                    con.execute(
                        """
                        UPDATE analysis_runs
                        SET status='partial', finished_at=?, error=?, coverage_json=?,
                            stats_json=?, report_json=?, report_markdown=?, report_sha256=?
                        WHERE id=?
                        """,
                        (
                            finished_at,
                            error,
                            coverage_json,
                            stats_json,
                            report_json,
                            report_markdown,
                            report_sha256,
                            run_id,
                        ),
                    )
                    row = con.execute(
                        "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
                    ).fetchone()
                    return self._run_with_citations(con, row)
                con.execute(
                    """
                    UPDATE analysis_runs
                    SET status=?, finished_at=?, error=?, coverage_json=?, stats_json=?
                    WHERE id=?
                    """,
                    (final_status, finished_at, error, coverage_json, stats_json, run_id),
                )
                row = con.execute(
                    "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
                return self._run_with_citations(con, row)

            if not report_markdown and report_value == {}:
                raise ValueError("成功的分析任务必须包含 report 或 report_markdown")

            self._insert_report_citations(con, run_id, citations, finished_at)
            con.execute(
                """
                UPDATE analysis_runs
                SET status='success', finished_at=?, error=NULL, coverage_json=?,
                    stats_json=?, report_json=?, report_markdown=?, report_sha256=?
                WHERE id=?
                """,
                (
                    finished_at,
                    coverage_json,
                    stats_json,
                    report_json,
                    report_markdown,
                    report_sha256,
                    run_id,
                ),
            )

            active = con.execute(
                "SELECT id, started_at FROM analysis_runs WHERE is_active=1"
            ).fetchone()
            should_activate = activate_requested and (
                active is None
                or (int(current["started_at"]), run_id)
                >= (int(active["started_at"]), int(active["id"]))
            )
            if should_activate:
                con.execute("UPDATE analysis_runs SET is_active=0 WHERE is_active=1")
                con.execute(
                    "UPDATE analysis_runs SET is_active=1 WHERE id=?", (run_id,)
                )
            row = con.execute(
                "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
            return self._run_with_citations(con, row)

    def latest_report(self, report_date: str | None = None) -> dict[str, Any] | None:
        with self.connection() as con:
            if report_date is None:
                row = con.execute(
                    """
                    SELECT * FROM analysis_runs
                    WHERE status='success' AND is_active=1
                    ORDER BY finished_at DESC, id DESC LIMIT 1
                    """
                ).fetchone()
            else:
                rows = con.execute(
                    """
                    SELECT * FROM analysis_runs
                    WHERE status='success' AND report_date=?
                    ORDER BY is_active DESC, finished_at DESC, id DESC
                    """,
                    (str(report_date),),
                ).fetchall()
                fallback: dict[str, Any] | None = None
                for item in rows:
                    decoded = self._run_with_citations(con, item)
                    fallback = fallback or decoded
                    config = decoded.get("config") or {}
                    report = decoded.get("report") or {}
                    mode = (
                        config.get("analysis_mode")
                        or report.get("analysis_mode")
                        or (
                            "historical_backfill"
                            if decoded.get("trigger") == "historical_backfill"
                            else "daily_current"
                        )
                    )
                    if mode == "daily_current":
                        return decoded
                return fallback
            return self._run_with_citations(con, row) if row else None

    def matching_successful_report(
        self,
        *,
        report_date: str,
        timezone: str,
        model_id: str,
        prompt_version: str,
        source_snapshot_hash: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find a success only when the complete immutable request matches."""
        expected_config = _json_value(_mapping(config))
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT * FROM analysis_runs
                WHERE status='success' AND report_date=? AND timezone=?
                  AND model_id=? AND prompt_version=? AND source_snapshot_hash=?
                ORDER BY finished_at DESC, id DESC
                """,
                (
                    str(report_date),
                    str(timezone),
                    str(model_id),
                    str(prompt_version),
                    str(source_snapshot_hash),
                ),
            ).fetchall()
            for row in rows:
                if str(row["config_json"]) == expected_config:
                    return self._run_with_citations(con, row)
        return None

    def latest_successful_report_for_mode(
        self, report_date: str, analysis_mode: str, *, timezone: str | None = None
    ) -> dict[str, Any] | None:
        with self.connection() as con:
            params: list[Any] = [str(report_date)]
            timezone_clause = ""
            if timezone is not None:
                timezone_clause = " AND timezone=?"
                params.append(str(timezone))
            rows = con.execute(
                """
                SELECT * FROM analysis_runs
                WHERE status='success' AND report_date=?
                """
                + timezone_clause
                + " ORDER BY finished_at DESC, id DESC",
                params,
            ).fetchall()
            for row in rows:
                decoded = self._run_with_citations(con, row)
                config = decoded.get("config") or {}
                report = decoded.get("report") or {}
                if (
                    config.get("analysis_mode") == analysis_mode
                    or report.get("analysis_mode") == analysis_mode
                ):
                    return decoded
        return None

    def matching_successful_report_for_mode(
        self,
        *,
        report_date: str,
        analysis_mode: str,
        timezone: str,
        model_id: str,
        prompt_version: str,
        source_snapshot_hash: str,
        config_requirements: Mapping[str, Any] | None = None,
        report_requirements: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find any successful mode run satisfying the immutable requirements.

        This deliberately scans all matching successes instead of filtering the
        newest run after selection. A later manual run with another model must
        not hide an earlier, fully compatible daily projection.
        """
        required_config = dict(config_requirements or {})
        required_report = dict(report_requirements or {})
        with self.connection() as con:
            rows = con.execute(
                """
                SELECT * FROM analysis_runs
                WHERE status='success' AND report_date=? AND timezone=?
                  AND model_id=? AND prompt_version=? AND source_snapshot_hash=?
                ORDER BY finished_at DESC, id DESC
                """,
                (
                    str(report_date),
                    str(timezone),
                    str(model_id),
                    str(prompt_version),
                    str(source_snapshot_hash),
                ),
            ).fetchall()
            for row in rows:
                decoded = self._run_with_citations(con, row)
                config = decoded.get("config") or {}
                report = decoded.get("report") or {}
                if not (
                    config.get("analysis_mode") == analysis_mode
                    or report.get("analysis_mode") == analysis_mode
                ):
                    continue
                if any(config.get(key) != value for key, value in required_config.items()):
                    continue
                if any(report.get(key) != value for key, value in required_report.items()):
                    continue
                return decoded
        return None

    def status(self) -> dict[str, Any]:
        with self.connection() as con:
            counts = {
                "runs": int(con.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]),
                "successful_runs": int(
                    con.execute(
                        "SELECT COUNT(*) FROM analysis_runs WHERE status='success'"
                    ).fetchone()[0]
                ),
                "evidence": int(con.execute("SELECT COUNT(*) FROM run_evidence").fetchone()[0]),
                "tasks": int(
                    con.execute(
                        "SELECT COUNT(*) FROM tasks WHERE task_key NOT LIKE 'archived:%'"
                    ).fetchone()[0]
                ),
                "archived_tasks": int(
                    con.execute(
                        "SELECT COUNT(*) FROM tasks WHERE task_key LIKE 'archived:%'"
                    ).fetchone()[0]
                ),
                "open_tasks": int(
                    con.execute(
                        """
                        SELECT COUNT(*) FROM tasks
                        WHERE status NOT IN ('done', 'canceled', 'superseded')
                        """
                    ).fetchone()[0]
                ),
                "opportunities": int(
                    con.execute(
                        """
                        SELECT COUNT(*) FROM opportunities
                        WHERE opportunity_key NOT LIKE 'archived:%'
                        """
                    ).fetchone()[0]
                ),
                "archived_opportunities": int(
                    con.execute(
                        """
                        SELECT COUNT(*) FROM opportunities
                        WHERE opportunity_key LIKE 'archived:%'
                        """
                    ).fetchone()[0]
                ),
            }
            latest = con.execute(
                "SELECT * FROM analysis_runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
            active = con.execute(
                "SELECT * FROM analysis_runs WHERE is_active=1 LIMIT 1"
            ).fetchone()
            latest_row = _decode_row(latest) if latest else None
            active_row = self._run_with_citations(con, active) if active else None
            dates = con.execute(
                """
                SELECT MIN(report_date) AS earliest, MAX(report_date) AS latest
                FROM analysis_runs WHERE status='success' AND report_date<>''
                """
            ).fetchone()
        return {
            **counts,
            "earliest_successful_report_date": dates["earliest"] if dates else None,
            "latest_successful_report_date": dates["latest"] if dates else None,
            "latest_run": latest_row,
            "latest_report": active_row,
        }

    def get_evidence(
        self, evidence: int | str | dict[str, Any]
    ) -> dict[str, Any] | None:
        with self.connection() as con:
            if isinstance(evidence, dict):
                if evidence.get("id") is not None or evidence.get("run_evidence_id") is not None:
                    value = _required_int(
                        evidence.get("run_evidence_id", evidence.get("id")), "evidence id"
                    )
                    return self._get_evidence_with_connection(con, value)
                run_id = evidence.get("run_id")
                evidence_key = evidence.get("evidence_key")
                if run_id is not None and evidence_key:
                    row = con.execute(
                        "SELECT id FROM run_evidence WHERE run_id=? AND evidence_key=?",
                        (int(run_id), str(evidence_key)),
                    ).fetchone()
                else:
                    source_key = _required_text(evidence.get("source_key"), "source_key")
                    row = con.execute(
                        """
                        SELECT re.id FROM run_evidence re
                        JOIN evidence_sources es ON es.id=re.evidence_source_id
                        WHERE es.source_key=? ORDER BY re.id DESC LIMIT 1
                        """,
                        (source_key,),
                    ).fetchone()
            elif isinstance(evidence, int):
                return self._get_evidence_with_connection(con, evidence)
            else:
                row = con.execute(
                    "SELECT id FROM run_evidence WHERE evidence_key=? ORDER BY id DESC LIMIT 1",
                    (str(evidence),),
                ).fetchone()
            if row is None:
                return None
            return self._get_evidence_with_connection(con, int(row["id"]))

    def upsert_task_observation(self, item: dict[str, Any]) -> dict[str, Any]:
        data = dict(item)
        task_data = dict(data.get("task") or {})
        for key in (
            "task_key",
            "dedupe_key",
            "title",
            "description",
            "project_key",
            "owner_key",
            "due_at",
        ):
            if key in data:
                task_data[key] = data[key]
        title = _required_text(task_data.get("title"), "task.title")
        task_key = str(
            task_data.get("task_key")
            or _stable_key(
                "task",
                task_data.get("project_key") or "",
                task_data.get("owner_key") or "",
                title.casefold(),
            )
        )
        observed_status_value = data.get("observed_status", data.get("status"))
        observed_status = (
            None if observed_status_value in (None, "") else str(observed_status_value)
        )
        if observed_status is not None:
            _validate_task_status(observed_status)
        confidence = _confidence(data.get("confidence"), default=None)
        observed_at = _optional_int(data.get("observed_at")) or _now_ms()
        run_id = _optional_int(data.get("run_id"))
        evidence_id = _optional_int(
            data.get("run_evidence_id", data.get("evidence_id"))
        )
        payload = _mapping(data.get("payload"))
        payload_json = _json_value(payload)
        observation_key = str(
            data.get("observation_key")
            or _stable_key(
                "task-observation",
                task_key,
                run_id,
                evidence_id,
                observed_status,
                confidence,
                payload_json,
            )
        )
        now = _optional_int(data.get("created_at")) or _now_ms()

        with self.transaction() as con:
            existing_observation = con.execute(
                """
                SELECT o.*, t.task_key FROM task_observations o
                JOIN tasks t ON t.id=o.task_id WHERE o.observation_key=?
                """,
                (observation_key,),
            ).fetchone()
            if existing_observation is not None:
                if not _same_observation(
                    existing_observation,
                    task_key=task_key,
                    run_id=run_id,
                    evidence_id=evidence_id,
                    observed_status=observed_status,
                    confidence=confidence,
                    payload_json=payload_json,
                ):
                    raise ValueError("observation_key 已被不同的任务观察占用")
                task = self._task_with_connection(
                    con, int(existing_observation["task_id"])
                )
                task["observation"] = _decode_row(existing_observation)
                task["applied"] = bool(existing_observation["applied"])
                return task

            self._validate_optional_links(con, run_id, evidence_id)
            task = con.execute(
                "SELECT * FROM tasks WHERE task_key=?", (task_key,)
            ).fetchone()
            task_created = task is None
            if task is None:
                initial_status = observed_status or "open"
                cursor = con.execute(
                    """
                    INSERT INTO tasks(
                        task_key, dedupe_key, title, description, project_key,
                        owner_key, due_at, status, status_source, confidence,
                        first_seen_at, last_seen_at, closed_at, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'machine', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_key,
                        str(task_data.get("dedupe_key") or ""),
                        title,
                        str(task_data.get("description") or ""),
                        str(task_data.get("project_key") or ""),
                        str(task_data.get("owner_key") or ""),
                        _optional_int(task_data.get("due_at")),
                        initial_status,
                        confidence or 0.0,
                        observed_at,
                        observed_at,
                        observed_at if _is_closed(initial_status) else None,
                        _json_value(_mapping(task_data.get("payload"))),
                        now,
                        now,
                    ),
                )
                task = con.execute(
                    "SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)
                ).fetchone()

            current_status = str(task["status"])
            manually_controlled = str(task["status_source"]) == "manual"
            latest_event = con.execute(
                """
                SELECT MAX(occurred_at) AS occurred_at FROM task_events
                WHERE task_id=? AND applied=1 AND to_status IS NOT NULL
                """,
                (int(task["id"]),),
            ).fetchone()
            latest_event_at = (
                int(latest_event["occurred_at"])
                if latest_event is not None and latest_event["occurred_at"] is not None
                else int(task["last_seen_at"])
            )
            projection_at = max(int(task["last_seen_at"]), latest_event_at)
            chronologically_current = task_created or observed_at > projection_at
            applied = chronologically_current and (
                not manually_controlled
                or observed_status is None
                or observed_status == current_status
            )
            new_status = observed_status if observed_status is not None and applied else current_status
            new_confidence = (
                confidence
                if confidence is not None and chronologically_current
                else float(task["confidence"])
            )
            closed_at = (
                observed_at
                if applied and _is_closed(new_status) and not _is_closed(current_status)
                else (None if not _is_closed(new_status) else task["closed_at"])
            )
            con.execute(
                """
                UPDATE tasks SET
                    dedupe_key=CASE WHEN ?=1 AND ?<>'' THEN ? ELSE dedupe_key END,
                    title=CASE WHEN ?=1 THEN ? ELSE title END,
                    description=CASE WHEN ?=1 AND ?<>'' THEN ? ELSE description END,
                    project_key=CASE WHEN ?=1 AND ?<>'' THEN ? ELSE project_key END,
                    owner_key=CASE WHEN ?=1 AND ?<>'' THEN ? ELSE owner_key END,
                    due_at=CASE WHEN ?=1 THEN COALESCE(?, due_at) ELSE due_at END,
                    status=?,
                    status_source=CASE WHEN status_source='manual' THEN 'manual' ELSE 'machine' END,
                    confidence=?, first_seen_at=MIN(first_seen_at, ?),
                    last_seen_at=MAX(last_seen_at, ?), closed_at=?,
                    version=version+1, updated_at=?
                WHERE id=?
                """,
                (
                    int(chronologically_current),
                    str(task_data.get("dedupe_key") or ""),
                    str(task_data.get("dedupe_key") or ""),
                    int(chronologically_current),
                    title,
                    int(chronologically_current),
                    str(task_data.get("description") or ""),
                    str(task_data.get("description") or ""),
                    int(chronologically_current),
                    str(task_data.get("project_key") or ""),
                    str(task_data.get("project_key") or ""),
                    int(chronologically_current),
                    str(task_data.get("owner_key") or ""),
                    str(task_data.get("owner_key") or ""),
                    int(chronologically_current),
                    _optional_int(task_data.get("due_at")),
                    new_status,
                    new_confidence,
                    observed_at,
                    observed_at,
                    closed_at,
                    now,
                    int(task["id"]),
                ),
            )
            cursor = con.execute(
                """
                INSERT INTO task_observations(
                    observation_key, task_id, run_id, run_evidence_id,
                    observed_status, confidence, applied, observed_at,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_key,
                    int(task["id"]),
                    run_id,
                    evidence_id,
                    observed_status,
                    confidence,
                    int(applied),
                    observed_at,
                    payload_json,
                    now,
                ),
            )
            event_payload_json = _json_value(
                {"observation_key": observation_key, "payload": payload}
            )
            con.execute(
                """
                INSERT INTO task_events(
                    task_id, run_id, event_key, event_type, actor_kind,
                    from_status, to_status, applied, occurred_at,
                    payload_json, created_at
                ) VALUES (?, ?, ?, 'observed', 'extractor', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(task["id"]),
                    run_id,
                    f"observation:{observation_key}",
                    current_status,
                    observed_status,
                    int(applied),
                    observed_at,
                    event_payload_json,
                    now,
                ),
            )
            result = self._task_with_connection(con, int(task["id"]))
            observation = con.execute(
                "SELECT * FROM task_observations WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            result["observation"] = _decode_row(observation)
            result["applied"] = applied
            return result

    def set_task_status(
        self,
        task: int | str | dict[str, Any],
        change: str | dict[str, Any] | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        if isinstance(task, dict):
            data = dict(task)
            selector: int | str | None = data.pop(
                "task_id", data.pop("id", data.get("task_key"))
            )
            if isinstance(change, dict):
                data.update(change)
            elif isinstance(change, str):
                data["status"] = change
        else:
            selector = task
            data = dict(change) if isinstance(change, dict) else {}
            if isinstance(change, str):
                data["status"] = change
        data.update(values)
        if selector is None:
            raise ValueError("缺少 task_id 或 task_key")
        new_status = _required_text(data.get("status"), "status")
        _validate_task_status(new_status)
        actor_raw = str(data.get("actor_kind", data.get("actor", "human"))).lower()
        actor_kind = "human" if actor_raw in {"human", "manual", "user"} else actor_raw
        if actor_kind not in {"human", "system", "extractor"}:
            raise ValueError(f"无效的任务事件主体: {actor_kind}")
        occurred_at = _optional_int(data.get("occurred_at")) or _now_ms()
        run_id = _optional_int(data.get("run_id"))
        payload = _mapping(data.get("payload"))
        if data.get("reason") is not None:
            payload = {**payload, "reason": str(data.get("reason"))}
        payload_json = _json_value(payload)
        explicit_event_key = data.get("event_key") or data.get("operation_id")
        now = _optional_int(data.get("created_at")) or _now_ms()

        with self.transaction() as con:
            task_row = self._find_task_with_connection(con, selector)
            if task_row is None:
                raise ValueError(f"任务不存在: {selector}")
            event_key = str(
                explicit_event_key
                or _stable_key(
                    "task-status",
                    int(task_row["id"]),
                    int(task_row["version"]),
                    str(task_row["status"]),
                    actor_kind,
                    new_status,
                    occurred_at,
                    payload_json,
                )
            )
            existing = con.execute(
                "SELECT * FROM task_events WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["task_id"]) != int(task_row["id"])
                    or str(existing["actor_kind"]) != actor_kind
                    or str(existing["to_status"] or "") != new_status
                    or str(existing["payload_json"]) != payload_json
                ):
                    raise ValueError("event_key 已被不同的任务事件占用")
                result = self._task_with_connection(con, int(task_row["id"]))
                result["event"] = _decode_row(existing)
                result["applied"] = bool(existing["applied"])
                return result

            if run_id is not None:
                self._validate_optional_links(con, run_id, None)
            current_status = str(task_row["status"])
            applied = actor_kind == "human" or str(task_row["status_source"]) != "manual"
            event_type = (
                "manual_status"
                if actor_kind == "human"
                else ("status_changed" if applied else "status_suppressed")
            )
            cursor = con.execute(
                """
                INSERT INTO task_events(
                    task_id, run_id, event_key, event_type, actor_kind,
                    from_status, to_status, applied, occurred_at,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(task_row["id"]),
                    run_id,
                    event_key,
                    event_type,
                    actor_kind,
                    current_status,
                    new_status,
                    int(applied),
                    occurred_at,
                    payload_json,
                    now,
                ),
            )
            if applied:
                closed_at = (
                    occurred_at
                    if _is_closed(new_status) and not _is_closed(current_status)
                    else (None if not _is_closed(new_status) else task_row["closed_at"])
                )
                con.execute(
                    """
                    UPDATE tasks SET status=?, status_source=?, closed_at=?,
                        version=version+1, updated_at=? WHERE id=?
                    """,
                    (
                        new_status,
                        "manual" if actor_kind == "human" else "machine",
                        closed_at,
                        now,
                        int(task_row["id"]),
                    ),
                )
            result = self._task_with_connection(con, int(task_row["id"]))
            event = con.execute(
                "SELECT * FROM task_events WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            result["event"] = _decode_row(event)
            result["applied"] = applied
            return result

    def reset_machine_projections(
        self,
        *,
        projection_version: str,
        include_current: bool = False,
    ) -> dict[str, int]:
        """Archive rebuildable machine projections while preserving their audit rows.

        Tasks or opportunities touched by a human are never removed. Their
        source reports and evidence also remain immutable; only the derived
        machine ledger and its projection rows are rebuilt chronologically.
        """
        expected = str(projection_version)
        now = _now_ms()
        with self.transaction() as con:
            task_rows: list[sqlite3.Row] = []
            for row in con.execute(
                """
                SELECT * FROM tasks
                WHERE status_source='machine' AND task_key NOT LIKE 'archived:%'
                """
            ).fetchall():
                payload = _json_load_mapping(str(row["payload_json"]))
                if include_current or payload.get("projection_version") != expected:
                    task_rows.append(row)
            for row in task_rows:
                task_id = int(row["id"])
                archived_key = f"archived:{expected}:task:{task_id}:{row['task_key']}"
                payload = _json_load_mapping(str(row["payload_json"]))
                con.execute(
                    """
                    INSERT INTO task_events(
                        task_id, run_id, event_key, event_type, actor_kind,
                        from_status, to_status, applied, occurred_at,
                        payload_json, created_at
                    ) VALUES (?, NULL, ?, 'projection_reset', 'system', ?,
                              'superseded', 1, ?, ?, ?)
                    """,
                    (
                        task_id,
                        f"projection-reset:{expected}:{task_id}",
                        str(row["status"]),
                        now,
                        _json_value(
                            {
                                "projection_version": expected,
                                "include_current": bool(include_current),
                                "previous_task_key": str(row["task_key"]),
                            }
                        ),
                        now,
                    ),
                )
                con.execute(
                    """
                    UPDATE tasks SET task_key=?, status='superseded',
                        closed_at=?, version=version+1, payload_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        archived_key,
                        now,
                        _json_value(
                            {
                                **payload,
                                "projection_archived": True,
                                "projection_archived_for": expected,
                                "projection_original_key": str(row["task_key"]),
                            }
                        ),
                        now,
                        task_id,
                    ),
                )

            opportunity_rows: list[sqlite3.Row] = []
            for row in con.execute(
                """
                SELECT * FROM opportunities
                WHERE status_source='machine' AND opportunity_key NOT LIKE 'archived:%'
                """
            ).fetchall():
                payload = _json_load_mapping(str(row["payload_json"]))
                if include_current or payload.get("projection_version") != expected:
                    opportunity_rows.append(row)
            for row in opportunity_rows:
                opportunity_id = int(row["id"])
                payload = _json_load_mapping(str(row["payload_json"]))
                con.execute(
                    """
                    UPDATE opportunities SET opportunity_key=?, status='expired',
                        payload_json=?, updated_at=? WHERE id=?
                    """,
                    (
                        f"archived:{expected}:opportunity:{opportunity_id}:{row['opportunity_key']}",
                        _json_value(
                            {
                                **payload,
                                "projection_archived": True,
                                "projection_archived_for": expected,
                                "projection_original_key": str(
                                    row["opportunity_key"]
                                ),
                                "projection_original_status": str(row["status"]),
                            }
                        ),
                        now,
                        opportunity_id,
                    ),
                )
        return {
            "tasks_archived": len(task_rows),
            "opportunities_archived": len(opportunity_rows),
            "include_current": int(bool(include_current)),
        }

    def replay_run_projections(
        self,
        run_id: int,
        *,
        campaign_id: str,
        projection_version: str,
    ) -> dict[str, int]:
        """Idempotently project a cached successful run into a rebuilt ledger."""
        with self.connection() as con:
            task_rows = con.execute(
                """
                SELECT o.id AS observation_id, o.run_evidence_id,
                       o.observed_status, o.confidence, o.observed_at,
                       o.payload_json AS observation_payload_json,
                       t.task_key, t.dedupe_key, t.title, t.description,
                       t.project_key, t.owner_key, t.due_at,
                       t.payload_json AS task_payload_json
                FROM task_observations o
                JOIN tasks t ON t.id=o.task_id
                WHERE o.run_id=? AND o.observation_key NOT LIKE 'replay:%'
                  AND t.task_key LIKE 'archived:%'
                ORDER BY o.observed_at, o.id
                """,
                (int(run_id),),
            ).fetchall()
            signal_rows = con.execute(
                """
                SELECT s.id AS signal_id, s.run_evidence_id, s.signal_kind,
                       s.score, s.confidence, s.observed_at,
                       s.payload_json AS signal_payload_json,
                       o.opportunity_key, o.entity_key, o.title, o.summary,
                       o.payload_json AS opportunity_payload_json
                FROM opportunity_signals s
                JOIN opportunities o ON o.id=s.opportunity_id
                WHERE s.run_id=? AND s.signal_key NOT LIKE 'replay:%'
                  AND o.opportunity_key LIKE 'archived:%'
                ORDER BY s.observed_at, s.id
                """,
                (int(run_id),),
            ).fetchall()

        task_count = 0
        for row in task_rows:
            task_payload = _json_load_mapping(str(row["task_payload_json"]))
            original_key = str(
                task_payload.get("projection_original_key") or row["task_key"]
            )
            clean_task_payload = {
                key: value
                for key, value in task_payload.items()
                if not key.startswith("projection_archived")
                and key != "projection_original_key"
            }
            clean_task_payload["projection_version"] = str(projection_version)
            self.upsert_task_observation(
                {
                    "run_id": int(run_id),
                    "evidence_id": _nullable_int(row["run_evidence_id"]),
                    "observation_key": (
                        f"replay:{campaign_id}:task:{int(row['observation_id'])}"
                    ),
                    "task": {
                        "task_key": original_key,
                        "dedupe_key": str(row["dedupe_key"]),
                        "title": str(row["title"]),
                        "description": str(row["description"]),
                        "project_key": str(row["project_key"]),
                        "owner_key": str(row["owner_key"]),
                        "due_at": _nullable_int(row["due_at"]),
                        "payload": clean_task_payload,
                    },
                    "observed_status": row["observed_status"],
                    "confidence": row["confidence"],
                    "observed_at": int(row["observed_at"]),
                    "payload": {
                        **_json_load_mapping(str(row["observation_payload_json"])),
                        "replayed_for_campaign": str(campaign_id),
                    },
                }
            )
            task_count += 1

        signal_count = 0
        for row in signal_rows:
            opportunity_payload = _json_load_mapping(
                str(row["opportunity_payload_json"])
            )
            original_key = str(
                opportunity_payload.get("projection_original_key")
                or row["opportunity_key"]
            )
            clean_opportunity_payload = {
                key: value
                for key, value in opportunity_payload.items()
                if not key.startswith("projection_archived")
                and key not in {"projection_original_key", "projection_original_status"}
            }
            clean_opportunity_payload["projection_version"] = str(
                projection_version
            )
            self.upsert_opportunity_signal(
                {
                    "run_id": int(run_id),
                    "evidence_id": _nullable_int(row["run_evidence_id"]),
                    "signal_key": (
                        f"replay:{campaign_id}:opportunity:{int(row['signal_id'])}"
                    ),
                    "opportunity": {
                        "opportunity_key": original_key,
                        "entity_key": str(row["entity_key"]),
                        "title": str(row["title"]),
                        "summary": str(row["summary"]),
                        "status": "active",
                        "payload": clean_opportunity_payload,
                    },
                    "signal_kind": str(row["signal_kind"]),
                    "score": float(row["score"]),
                    "confidence": float(row["confidence"]),
                    "observed_at": int(row["observed_at"]),
                    "payload": {
                        **_json_load_mapping(str(row["signal_payload_json"])),
                        "replayed_for_campaign": str(campaign_id),
                    },
                }
            )
            signal_count += 1
        return {
            "task_observations_replayed": task_count,
            "opportunity_signals_replayed": signal_count,
        }

    def list_tasks(
        self,
        *,
        status: str | None = None,
        open_only: bool = False,
        project_key: str | None = None,
        owner_key: str | None = None,
        limit: int | None = 200,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("task_key NOT LIKE 'archived:%'")
        if status:
            _validate_task_status(status)
            clauses.append("status=?")
            params.append(status)
        if open_only:
            if status is not None:
                raise ValueError("status 与 open_only 不能同时使用")
            clauses.append("status NOT IN ('done', 'canceled', 'superseded')")
        if project_key is not None:
            clauses.append("project_key=?")
            params.append(str(project_key))
        if owner_key is not None:
            clauses.append("owner_key=?")
            params.append(str(owner_key))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as con:
            sql = (
                """
                SELECT t.*,
                       (
                           SELECT o.run_id FROM task_observations o
                           WHERE o.task_id=t.id
                           ORDER BY o.observed_at DESC, o.id DESC LIMIT 1
                       ) AS latest_observation_run_id
                FROM tasks t
                """
                + where
                + " ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, "
                "due_at, last_seen_at DESC, id DESC"
            )
            if limit is None:
                rows = con.execute(sql, params).fetchall()
            else:
                bounded_limit = max(1, min(int(limit), 10_000))
                rows = con.execute(sql + " LIMIT ?", (*params, bounded_limit)).fetchall()
        return [_decode_row(row) for row in rows]

    def upsert_opportunity_signal(self, item: dict[str, Any]) -> dict[str, Any]:
        data = dict(item)
        opportunity_data = dict(data.get("opportunity") or {})
        for key in (
            "opportunity_key",
            "entity_key",
            "title",
            "summary",
            "status",
        ):
            if key in data:
                opportunity_data[key] = data[key]
        title = _required_text(opportunity_data.get("title"), "opportunity.title")
        opportunity_key = str(
            opportunity_data.get("opportunity_key")
            or _stable_key(
                "opportunity",
                opportunity_data.get("entity_key") or "",
                title.casefold(),
            )
        )
        run_id = _optional_int(data.get("run_id"))
        evidence_id = _optional_int(
            data.get("run_evidence_id", data.get("evidence_id"))
        )
        score = _finite_float(data.get("score"), default=0.0)
        confidence = _confidence(data.get("confidence"), default=0.0)
        observed_at = _optional_int(data.get("observed_at")) or _now_ms()
        payload = _mapping(data.get("payload"))
        payload_json = _json_value(payload)
        signal_key = str(
            data.get("signal_key")
            or _stable_key(
                "opportunity-signal",
                opportunity_key,
                run_id,
                evidence_id,
                data.get("signal_kind") or "mention",
                score,
                confidence,
                payload_json,
            )
        )
        now = _optional_int(data.get("created_at")) or _now_ms()

        with self.transaction() as con:
            existing_signal = con.execute(
                """
                SELECT s.*, o.opportunity_key FROM opportunity_signals s
                JOIN opportunities o ON o.id=s.opportunity_id
                WHERE s.signal_key=?
                """,
                (signal_key,),
            ).fetchone()
            if existing_signal is not None:
                if (
                    str(existing_signal["opportunity_key"]) != opportunity_key
                    or _nullable_int(existing_signal["run_id"]) != run_id
                    or _nullable_int(existing_signal["run_evidence_id"]) != evidence_id
                    or str(existing_signal["payload_json"]) != payload_json
                    or float(existing_signal["score"]) != score
                    or float(existing_signal["confidence"]) != confidence
                ):
                    raise ValueError("signal_key 已被不同的机会信号占用")
                result = self._opportunity_with_connection(
                    con, int(existing_signal["opportunity_id"])
                )
                result["signal"] = _decode_row(existing_signal)
                result["created"] = False
                return result

            self._validate_optional_links(con, run_id, evidence_id)
            opportunity = con.execute(
                "SELECT * FROM opportunities WHERE opportunity_key=?",
                (opportunity_key,),
            ).fetchone()
            opportunity_created = opportunity is None
            if opportunity is None:
                requested_status = str(opportunity_data.get("status") or "active")
                if requested_status not in _OPPORTUNITY_STATUSES:
                    raise ValueError(f"无效的机会状态: {requested_status}")
                cursor = con.execute(
                    """
                    INSERT INTO opportunities(
                        opportunity_key, entity_key, title, summary, status,
                        score, confidence, signal_count, first_seen_at, last_seen_at,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        opportunity_key,
                        str(opportunity_data.get("entity_key") or ""),
                        title,
                        str(opportunity_data.get("summary") or ""),
                        requested_status,
                        score,
                        confidence,
                        observed_at,
                        observed_at,
                        _json_value(_mapping(opportunity_data.get("payload"))),
                        now,
                        now,
                    ),
                )
                opportunity = con.execute(
                    "SELECT * FROM opportunities WHERE id=?", (cursor.lastrowid,)
                ).fetchone()

            cursor = con.execute(
                """
                INSERT INTO opportunity_signals(
                    signal_key, opportunity_id, run_id, run_evidence_id,
                    signal_kind, score, confidence, observed_at,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_key,
                    int(opportunity["id"]),
                    run_id,
                    evidence_id,
                    str(data.get("signal_kind") or "mention"),
                    score,
                    confidence,
                    observed_at,
                    payload_json,
                    now,
                ),
            )
            common_update = (
                "signal_count=signal_count+1, "
                "first_seen_at=MIN(first_seen_at, ?), "
                "last_seen_at=MAX(last_seen_at, ?), updated_at=?"
            )
            chronologically_current = (
                opportunity_created
                or observed_at > int(opportunity["last_seen_at"])
            )
            if (
                str(opportunity["status_source"]) != "manual"
                and chronologically_current
            ):
                con.execute(
                    f"""
                    UPDATE opportunities SET
                        entity_key=CASE WHEN ?='' THEN entity_key ELSE ? END,
                        title=?, summary=CASE WHEN ?='' THEN summary ELSE ? END,
                        score=?, confidence=?, {common_update}
                    WHERE id=?
                    """,
                    (
                        str(opportunity_data.get("entity_key") or ""),
                        str(opportunity_data.get("entity_key") or ""),
                        title,
                        str(opportunity_data.get("summary") or ""),
                        str(opportunity_data.get("summary") or ""),
                        score,
                        confidence,
                        observed_at,
                        observed_at,
                        now,
                        int(opportunity["id"]),
                    ),
                )
            else:
                con.execute(
                    f"UPDATE opportunities SET {common_update} WHERE id=?",
                    (observed_at, observed_at, now, int(opportunity["id"])),
                )
            result = self._opportunity_with_connection(con, int(opportunity["id"]))
            signal = con.execute(
                "SELECT * FROM opportunity_signals WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            result["signal"] = _decode_row(signal)
            result["created"] = True
            return result

    def list_opportunities(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        clauses: list[str] = []
        if not include_archived:
            clauses.append("opportunity_key NOT LIKE 'archived:%'")
        if status is not None:
            if status not in _OPPORTUNITY_STATUSES:
                raise ValueError(f"无效的机会状态: {status}")
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        bounded_limit = max(1, min(int(limit), 2000))
        with self.connection() as con:
            rows = con.execute(
                "SELECT * FROM opportunities"
                + where
                + " ORDER BY score DESC, confidence DESC, last_seen_at DESC, id DESC LIMIT ?",
                (*params, bounded_limit),
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def _insert_report_citations(
        self,
        con: sqlite3.Connection,
        run_id: int,
        citations: list[Any],
        created_at: int,
    ) -> None:
        for position, raw in enumerate(citations):
            if not isinstance(raw, dict):
                raise ValueError("每条 citation 必须是对象")
            item = dict(raw)
            evidence_id = _optional_int(
                item.get("run_evidence_id", item.get("evidence_id"))
            )
            if evidence_id is None and item.get("evidence_key"):
                evidence_row = con.execute(
                    "SELECT id FROM run_evidence WHERE run_id=? AND evidence_key=?",
                    (run_id, str(item["evidence_key"])),
                ).fetchone()
                evidence_id = int(evidence_row["id"]) if evidence_row else None
            if evidence_id is None:
                raise ValueError("citation 缺少有效的 evidence_id/evidence_key")
            evidence_row = con.execute(
                "SELECT id FROM run_evidence WHERE id=? AND run_id=?",
                (evidence_id, run_id),
            ).fetchone()
            if evidence_row is None:
                raise ValueError("citation 只能引用同一分析任务的证据")
            ordinal = _optional_int(item.get("ordinal"))
            if ordinal is None:
                ordinal = position
            citation_key = str(
                item.get("citation_key")
                or _stable_key(
                    "citation",
                    run_id,
                    evidence_id,
                    item.get("section") or "",
                    item.get("claim_key") or "",
                    ordinal,
                )
            )
            metadata_json = _json_value(_mapping(item.get("metadata")))
            existing = con.execute(
                """
                SELECT * FROM report_citations
                WHERE run_id=? AND citation_key=?
                """,
                (run_id, citation_key),
            ).fetchone()
            expected = (
                evidence_id,
                str(item.get("section") or ""),
                str(item.get("claim_key") or ""),
                str(item.get("claim_text") or ""),
                ordinal,
                metadata_json,
            )
            if existing is not None:
                actual = (
                    int(existing["run_evidence_id"]),
                    str(existing["section"]),
                    str(existing["claim_key"]),
                    str(existing["claim_text"]),
                    int(existing["ordinal"]),
                    str(existing["metadata_json"]),
                )
                if actual != expected:
                    raise ValueError("citation_key 已被不同的引用占用")
                continue
            con.execute(
                """
                INSERT INTO report_citations(
                    run_id, run_evidence_id, citation_key, section, claim_key,
                    claim_text, ordinal, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, evidence_id, citation_key, *expected[1:], created_at),
            )

    def _run_with_citations(
        self, con: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        result = _decode_row(row)
        citations = con.execute(
            """
            SELECT rc.*, re.evidence_key
            FROM report_citations rc
            JOIN run_evidence re ON re.id=rc.run_evidence_id
            WHERE rc.run_id=? ORDER BY rc.section, rc.ordinal, rc.id
            """,
            (int(row["id"]),),
        ).fetchall()
        result["citations"] = [_decode_row(item) for item in citations]
        return result

    def _get_evidence_with_connection(
        self, con: sqlite3.Connection, evidence_id: int
    ) -> dict[str, Any] | None:
        row = con.execute(
            """
            SELECT
                re.id, re.id AS run_evidence_id, re.run_id, re.evidence_key,
                re.span_start, re.span_end, re.excerpt_text, re.excerpt_sha256,
                re.metadata_json, re.created_at,
                es.id AS evidence_source_id, es.source_key, es.source_kind,
                es.source_id, es.source_version, es.container_id, es.title,
                es.author, es.occurred_at, es.content_text, es.content_sha256,
                es.metadata_json AS source_metadata_json,
                es.created_at AS source_created_at
            FROM run_evidence re
            JOIN evidence_sources es ON es.id=re.evidence_source_id
            WHERE re.id=?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        result = _decode_row(row)
        result["source_metadata"] = _json_load(str(row["source_metadata_json"]))
        return result

    def _validate_optional_links(
        self,
        con: sqlite3.Connection,
        run_id: int | None,
        evidence_id: int | None,
    ) -> None:
        if run_id is not None:
            if con.execute(
                "SELECT 1 FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"分析任务不存在: {run_id}")
        if evidence_id is not None:
            row = con.execute(
                "SELECT run_id FROM run_evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"证据不存在: {evidence_id}")
            if run_id is not None and int(row["run_id"]) != run_id:
                raise ValueError("证据不属于指定的分析任务")

    def _find_task_with_connection(
        self, con: sqlite3.Connection, selector: int | str
    ) -> sqlite3.Row | None:
        if isinstance(selector, int) or str(selector).isdigit():
            return con.execute(
                "SELECT * FROM tasks WHERE id=?", (int(selector),)
            ).fetchone()
        return con.execute(
            "SELECT * FROM tasks WHERE task_key=?", (str(selector),)
        ).fetchone()

    def _task_with_connection(
        self, con: sqlite3.Connection, task_id: int
    ) -> dict[str, Any]:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"任务不存在: {task_id}")
        return _decode_row(row)

    def _opportunity_with_connection(
        self, con: sqlite3.Connection, opportunity_id: int
    ) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"机会不存在: {opportunity_id}")
        return _decode_row(row)


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    result = dict(row)
    for column, alias in _JSON_COLUMNS.items():
        if column in result and result[column] is not None:
            result[alias] = _json_load(str(result[column]))
    if "source_metadata_json" in result and result["source_metadata_json"] is not None:
        result["source_metadata"] = _json_load(str(result["source_metadata_json"]))
    if "is_active" in result:
        result["is_active"] = bool(result["is_active"])
    if "applied" in result:
        result["applied"] = bool(result["applied"])
    return result


def _mapping(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        return _json_load_mapping(value)
    if not isinstance(value, dict):
        raise ValueError("JSON 字段必须是对象")
    return dict(value)


def _json_load_mapping(value: str) -> dict[str, Any]:
    parsed = _json_load(value)
    if not isinstance(parsed, dict):
        raise ValueError("JSON 字段必须是对象")
    return parsed


def _json_load(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("数据库中存在无效 JSON") from exc


def _json_value(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无法安全序列化 JSON 字段") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_key(prefix: str, *parts: Any) -> str:
    return f"{prefix}:{_sha256_text(_json_value(list(parts)))}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效整数: {value}") from exc


def _required_int(value: Any, name: str) -> int:
    result = _optional_int(value)
    if result is None:
        raise ValueError(f"缺少 {name}")
    return result


def _required_text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"缺少 {name}")
    return result


def _finite_float(value: Any, *, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效数值: {value}") from exc
    if not math.isfinite(result):
        raise ValueError("数值必须是有限值")
    return result


def _confidence(value: Any, *, default: float | None) -> float | None:
    if value in (None, ""):
        return default
    result = _finite_float(value, default=0.0)
    if result < 0.0 or result > 1.0:
        raise ValueError("confidence 必须在 0 到 1 之间")
    return result


def _nullable_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _validate_task_status(status: str) -> None:
    if status not in _TASK_STATUSES:
        raise ValueError(f"无效的任务状态: {status}")


def _is_closed(status: str) -> bool:
    return status in {"done", "canceled", "superseded"}


def _same_evidence(
    row: dict[str, Any] | None,
    *,
    source_kind: str,
    source_id: str,
    source_version: str,
    content_sha256: str,
    span_start: int,
    span_end: int,
    excerpt_sha256: str,
) -> bool:
    return bool(
        row
        and row.get("source_kind") == source_kind
        and row.get("source_id") == source_id
        and row.get("source_version") == source_version
        and row.get("content_sha256") == content_sha256
        and int(row.get("span_start", -1)) == span_start
        and int(row.get("span_end", -1)) == span_end
        and row.get("excerpt_sha256") == excerpt_sha256
    )


def _same_finished_run(
    row: sqlite3.Row,
    *,
    status: str,
    report_sha256: str,
    error: str | None,
) -> bool:
    if str(row["status"]) != status:
        return False
    if status == "success":
        return str(row["report_sha256"] or "") == report_sha256
    return (None if row["error"] is None else str(row["error"])) == error


def _same_observation(
    row: sqlite3.Row,
    *,
    task_key: str,
    run_id: int | None,
    evidence_id: int | None,
    observed_status: str | None,
    confidence: float | None,
    payload_json: str,
) -> bool:
    return (
        str(row["task_key"]) == task_key
        and _nullable_int(row["run_id"]) == run_id
        and _nullable_int(row["run_evidence_id"]) == evidence_id
        and (None if row["observed_status"] is None else str(row["observed_status"]))
        == observed_status
        and (None if row["confidence"] is None else float(row["confidence"]))
        == confidence
        and str(row["payload_json"]) == payload_json
    )
