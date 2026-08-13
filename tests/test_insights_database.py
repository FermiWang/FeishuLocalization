import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from feishu_archive.insights_database import (
    INSIGHTS_SCHEMA_VERSION,
    InsightsDatabase,
)


class InsightsDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "insights.sqlite3"
        self.database = InsightsDatabase(self.path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start_run(self, key: str, started_at: int = 100) -> dict:
        return self.database.start_run(
            {
                "run_key": key,
                "trigger": "manual",
                "report_date": "2026-08-13",
                "timezone": "Europe/Amsterdam",
                "window_start": 1,
                "window_end": 2,
                "snapshot_at": 3,
                "source_snapshot_hash": key,
                "prompt_version": "v1",
                "model_id": "local-test",
                "config": {"language": "zh-CN"},
                "coverage": {"mail": "complete"},
                "started_at": started_at,
            }
        )

    def add_evidence(self, run_id: int, key: str = "ev-1") -> dict:
        return self.database.add_evidence(
            run_id,
            {
                "evidence_key": key,
                "source_kind": "mail",
                "source_id": f"message-{key}",
                "source_version": "1",
                "container_id": "INBOX",
                "title": "本地邮箱归档",
                "content_text": "邮件正文：请在周五完成归档。",
                "excerpt_text": "请在周五完成归档",
                "span_start": 5,
                "span_end": 15,
                "metadata": {"folder": "所有邮件"},
            },
        )

    def test_migration_pragmas_permissions_and_future_guard(self) -> None:
        self.assertEqual(self.database.schema_version(), INSIGHTS_SCHEMA_VERSION)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.database.initialize()
        self.assertEqual(self.database.integrity_check(), "ok")

        with self.database.connection() as con:
            self.assertEqual(str(con.execute("PRAGMA journal_mode").fetchone()[0]), "wal")
            self.assertEqual(int(con.execute("PRAGMA synchronous").fetchone()[0]), 1)
            self.assertEqual(int(con.execute("PRAGMA foreign_keys").fetchone()[0]), 1)
            self.assertEqual(int(con.execute("PRAGMA secure_delete").fetchone()[0]), 1)
            self.assertEqual(int(con.execute("PRAGMA busy_timeout").fetchone()[0]), 30_000)
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertTrue(
            {
                "analysis_runs",
                "evidence_sources",
                "run_evidence",
                "tasks",
                "task_events",
                "task_observations",
                "opportunity_signals",
                "opportunities",
                "report_citations",
            }.issubset(tables)
        )

        future_path = Path(self.temp.name) / "future.sqlite3"
        with sqlite3.connect(future_path) as con:
            con.execute(f"PRAGMA user_version={INSIGHTS_SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "高于程序支持版本"):
            InsightsDatabase(future_path).initialize()

    def test_runs_and_evidence_are_idempotent_and_json_is_safe(self) -> None:
        first = self.start_run("same-run")
        second = self.start_run("same-run", started_at=999)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["config"], {"language": "zh-CN"})
        self.assertEqual(second["coverage"], {"mail": "complete"})

        evidence = self.add_evidence(first["id"])
        same_evidence = self.add_evidence(first["id"])
        self.assertEqual(evidence["id"], same_evidence["id"])
        self.assertEqual(evidence["metadata"], {"folder": "所有邮件"})
        self.assertEqual(self.database.get_evidence("ev-1")["id"], evidence["id"])

        with self.assertRaisesRegex(ValueError, "不同的证据"):
            self.database.add_evidence(
                first["id"],
                {
                    "evidence_key": "ev-1",
                    "source_kind": "mail",
                    "source_id": "message-ev-1",
                    "source_version": "1",
                    "content_text": "被替换的内容",
                },
            )

        with self.assertRaisesRegex(ValueError, "安全序列化"):
            self.database.start_run(
                {"run_key": "nan-run", "config": {"bad": float("nan")}}
            )

        same_snapshot_request = self.database.start_run(
            {
                "run_key": "snapshot-independent",
                "report_date": "2026-08-12",
                "snapshot_at": 100,
                "source_snapshot_hash": "stable",
            }
        )
        retried_snapshot_request = self.database.start_run(
            {
                "run_key": "snapshot-independent",
                "report_date": "2026-08-12",
                "snapshot_at": 999,
                "source_snapshot_hash": "stable",
            }
        )
        self.assertEqual(same_snapshot_request["id"], retried_snapshot_request["id"])

    def test_finish_run_activates_atomically_and_failure_keeps_last_success(self) -> None:
        first = self.start_run("first", started_at=100)
        first_evidence = self.add_evidence(first["id"], "ev-first")
        completed = self.database.finish_run(
            first["id"],
            {
                "status": "success",
                "report": {"summary": "第一版"},
                "report_markdown": "# 第一版",
                "coverage": {"mail": "complete"},
                "citations": [
                    {
                        "citation_key": "citation-first",
                        "evidence_id": first_evidence["id"],
                        "section": "yesterday",
                        "claim_text": "已收到归档要求",
                    }
                ],
            },
        )
        self.assertTrue(completed["is_active"])
        self.assertEqual(self.database.latest_report()["report"], {"summary": "第一版"})

        failed = self.start_run("failed", started_at=200)
        self.database.finish_run(failed["id"], {"status": "error", "error": "模型超时"})
        self.assertEqual(self.database.latest_report()["id"], first["id"])

        invalid = self.start_run("invalid", started_at=300)
        with self.assertRaisesRegex(ValueError, "同一分析任务"):
            self.database.finish_run(
                invalid["id"],
                {
                    "status": "success",
                    "report": {"summary": "不应发布"},
                    "citations": [{"evidence_id": first_evidence["id"]}],
                },
            )
        self.assertEqual(self.database.latest_report()["id"], first["id"])
        self.assertEqual(self.database.status()["latest_run"]["status"], "running")

        invalid_evidence = self.add_evidence(invalid["id"], "ev-invalid")
        newer = self.database.finish_run(
            invalid["id"],
            {
                "status": "success",
                "report": {"summary": "第三版"},
                "citations": [{"evidence_id": invalid_evidence["id"]}],
            },
        )
        self.assertTrue(newer["is_active"])
        self.assertEqual(self.database.latest_report()["id"], invalid["id"])

        same = self.database.finish_run(
            invalid["id"],
            {
                "status": "success",
                "report": {"summary": "第三版"},
                "citations": [{"evidence_id": invalid_evidence["id"]}],
            },
        )
        self.assertEqual(same["id"], invalid["id"])

    def test_manual_task_status_has_priority_and_events_are_append_only(self) -> None:
        run = self.start_run("tasks")
        evidence = self.add_evidence(run["id"], "task-evidence")
        task = self.database.upsert_task_observation(
            {
                "observation_key": "observe-open",
                "task": {
                    "task_key": "task-archive",
                    "title": "完成邮箱归档",
                    "project_key": "feishu",
                },
                "run_id": run["id"],
                "evidence_id": evidence["id"],
                "observed_status": "open",
                "confidence": 0.9,
            }
        )
        manual = self.database.set_task_status(
            task["id"],
            {
                "status": "done",
                "actor_kind": "human",
                "event_key": "manual-done-1",
                "reason": "用户确认完成",
            },
        )
        self.assertEqual(manual["status"], "done")
        self.assertEqual(manual["status_source"], "manual")

        suppressed = self.database.upsert_task_observation(
            {
                "observation_key": "observe-again",
                "task": {"task_key": "task-archive", "title": "完成邮箱归档"},
                "run_id": run["id"],
                "evidence_id": evidence["id"],
                "observed_status": "open",
            }
        )
        self.assertEqual(suppressed["status"], "done")
        self.assertFalse(suppressed["applied"])

        duplicate = self.database.set_task_status(
            task["id"],
            {
                "status": "done",
                "actor_kind": "human",
                "event_key": "manual-done-1",
                "reason": "用户确认完成",
            },
        )
        self.assertEqual(duplicate["status"], "done")
        with self.database.connection() as con:
            self.assertEqual(
                int(con.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]), 3
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute(
                    "UPDATE task_events SET event_type='changed' WHERE event_key='manual-done-1'"
                )

        reopened = self.database.set_task_status(
            {
                "task_key": "task-archive",
                "status": "open",
                "event_key": "manual-reopen-1",
            }
        )
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(self.database.list_tasks(status="open")[0]["task_key"], "task-archive")

        closed_again = self.database.set_task_status(
            {"task_key": "task-archive", "status": "done"}
        )
        self.assertEqual(closed_again["status"], "done")

    def test_opportunity_signal_is_idempotent(self) -> None:
        run = self.start_run("opportunities")
        evidence = self.add_evidence(run["id"], "opportunity-evidence")
        item = {
            "signal_key": "signal-1",
            "opportunity": {
                "opportunity_key": "opportunity-mail",
                "entity_key": "Feishu Mail",
                "title": "同步全部邮箱文件夹",
                "summary": "不再限制最近 30 天",
            },
            "run_id": run["id"],
            "evidence_id": evidence["id"],
            "score": 0.8,
            "confidence": 0.9,
            "payload": {"folders": "all"},
        }
        first = self.database.upsert_opportunity_signal(item)
        second = self.database.upsert_opportunity_signal(item)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["signal_count"], 1)
        self.assertEqual(self.database.list_opportunities()[0]["opportunity_key"], "opportunity-mail")


if __name__ == "__main__":
    unittest.main()
