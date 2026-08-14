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

    def test_matching_success_requires_snapshot_timezone_and_full_config(self) -> None:
        run = self.start_run("matching-run")
        self.database.finish_run(
            run["id"],
            status="success",
            report={"analysis_mode": "historical_backfill"},
        )
        match = self.database.matching_successful_report(
            report_date="2026-08-13",
            timezone="Europe/Amsterdam",
            model_id="local-test",
            prompt_version="v1",
            source_snapshot_hash="matching-run",
            config={"language": "zh-CN"},
        )
        self.assertEqual(match["id"], run["id"])
        self.assertIsNone(
            self.database.matching_successful_report(
                report_date="2026-08-13",
                timezone="Europe/Amsterdam",
                model_id="local-test",
                prompt_version="v1",
                source_snapshot_hash="changed-snapshot",
                config={"language": "zh-CN"},
            )
        )
        self.assertIsNone(
            self.database.matching_successful_report(
                report_date="2026-08-13",
                timezone="Europe/Amsterdam",
                model_id="local-test",
                prompt_version="v1",
                source_snapshot_hash="matching-run",
                config={"language": "zh-CN", "max_chunk_chars": 1},
            )
        )

        with self.assertRaisesRegex(ValueError, "安全序列化"):
            self.database.start_run(
                {"run_key": "nan-run", "config": {"bad": float("nan")}}
            )

    def test_date_lookup_prefers_daily_current_over_newer_history_run(self) -> None:
        daily = self.database.start_run(
            run_key="daily-date",
            report_date="2026-08-10",
            timezone="Europe/Amsterdam",
            trigger="scheduled",
            config={"analysis_mode": "daily_current"},
            started_at=100,
        )
        self.database.finish_run(
            daily["id"], status="success", report={"analysis_mode": "daily_current"}
        )
        newer_active = self.database.start_run(
            run_key="daily-newer",
            report_date="2026-08-11",
            timezone="Europe/Amsterdam",
            trigger="scheduled",
            config={"analysis_mode": "daily_current"},
            started_at=200,
        )
        self.database.finish_run(
            newer_active["id"],
            status="success",
            report={"analysis_mode": "daily_current"},
        )
        history = self.database.start_run(
            run_key="history-date",
            report_date="2026-08-10",
            timezone="Europe/Amsterdam",
            trigger="historical_backfill",
            config={"analysis_mode": "historical_backfill"},
            started_at=300,
        )
        self.database.finish_run(
            history["id"],
            status="success",
            report={"analysis_mode": "historical_backfill"},
            activate=False,
        )
        selected = self.database.latest_report("2026-08-10")
        self.assertEqual(selected["id"], daily["id"])

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

    def test_failed_base_run_reuses_successful_retry_with_same_identity(self) -> None:
        base = self.database.start_run(
            run_key="retry-base",
            report_date="2026-08-12",
            source_snapshot_hash="snapshot",
            prompt_version="v3",
            model_id="model",
            config={"max_chunk_chars": 24000},
        )
        self.database.finish_run(base["id"], status="error", error="first attempt")
        retry = self.database.start_run(
            run_key="retry-base:retry:1",
            report_date="2026-08-12",
            source_snapshot_hash="snapshot",
            prompt_version="v3",
            model_id="model",
            config={"max_chunk_chars": 24000},
        )
        self.database.finish_run(
            retry["id"], status="success", report={"summary": "usable"}
        )

        reusable = self.database.find_reusable_run(base)
        self.assertIsNotNone(reusable)
        self.assertEqual(reusable["id"], retry["id"])
        self.assertEqual(reusable["report"], {"summary": "usable"})

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

    def test_out_of_order_history_does_not_regress_current_task_projection(self) -> None:
        current = self.database.upsert_task_observation(
            {
                "task": {"title": "提交项目资料", "description": "当前描述"},
                "observed_status": "open",
                "confidence": 0.9,
                "observed_at": 200,
            }
        )
        historical = self.database.upsert_task_observation(
            {
                "task": {"title": "提交项目资料", "description": "旧描述"},
                "observed_status": "done",
                "confidence": 0.2,
                "observed_at": 100,
            }
        )
        self.assertFalse(historical["applied"])
        self.assertEqual(historical["status"], "open")
        self.assertEqual(historical["description"], "当前描述")
        self.assertEqual(historical["confidence"], 0.9)
        self.assertEqual(historical["first_seen_at"], 100)
        self.assertEqual(historical["last_seen_at"], 200)

        manual = self.database.set_task_status(
            current["id"], "done", actor_kind="human", occurred_at=300
        )
        self.assertEqual(manual["status"], "done")
        machine = self.database.upsert_task_observation(
            {
                "task": {"title": "提交项目资料"},
                "observed_status": "open",
                "observed_at": 400,
            }
        )
        self.assertFalse(machine["applied"])
        self.assertEqual(machine["status"], "done")
        self.assertEqual(machine["status_source"], "manual")

    def test_equal_timestamp_does_not_replace_task_projection(self) -> None:
        current = self.database.upsert_task_observation(
            {
                "task": {"title": "提交同刻资料", "description": "先写入描述"},
                "observed_status": "open",
                "confidence": 0.9,
                "observed_at": 200,
            }
        )
        self.assertTrue(current["applied"])

        conflicting = self.database.upsert_task_observation(
            {
                "task": {"title": "提交同刻资料", "description": "后写入描述"},
                "observed_status": "done",
                "confidence": 0.2,
                "observed_at": 200,
            }
        )
        self.assertFalse(conflicting["applied"])
        self.assertEqual(conflicting["status"], "open")
        self.assertEqual(conflicting["description"], "先写入描述")
        self.assertEqual(conflicting["confidence"], 0.9)
        self.assertEqual(conflicting["first_seen_at"], 200)
        self.assertEqual(conflicting["last_seen_at"], 200)

    def test_out_of_order_history_does_not_regress_opportunity_projection(self) -> None:
        self.database.upsert_opportunity_signal(
            {
                "opportunity": {
                    "entity_key": "客户甲",
                    "title": "尽调服务",
                    "summary": "当前机会",
                },
                "score": 1.0,
                "confidence": 0.9,
                "observed_at": 200,
            }
        )
        historical = self.database.upsert_opportunity_signal(
            {
                "opportunity": {
                    "entity_key": "客户甲",
                    "title": "尽调服务",
                    "summary": "较旧弱信号",
                },
                "score": 0.25,
                "confidence": 0.3,
                "observed_at": 100,
            }
        )
        self.assertEqual(historical["summary"], "当前机会")
        self.assertEqual(historical["score"], 1.0)
        self.assertEqual(historical["confidence"], 0.9)
        self.assertEqual(historical["signal_count"], 2)
        self.assertEqual(historical["first_seen_at"], 100)
        self.assertEqual(historical["last_seen_at"], 200)

    def test_equal_timestamp_does_not_replace_opportunity_projection(self) -> None:
        current = self.database.upsert_opportunity_signal(
            {
                "opportunity": {
                    "entity_key": "客户乙",
                    "title": "同刻尽调服务",
                    "summary": "先写入机会",
                },
                "score": 1.0,
                "confidence": 0.9,
                "observed_at": 200,
            }
        )
        self.assertEqual(current["summary"], "先写入机会")

        conflicting = self.database.upsert_opportunity_signal(
            {
                "opportunity": {
                    "entity_key": "客户乙",
                    "title": "同刻尽调服务",
                    "summary": "后写入弱信号",
                },
                "score": 0.25,
                "confidence": 0.3,
                "observed_at": 200,
            }
        )
        self.assertEqual(conflicting["summary"], "先写入机会")
        self.assertEqual(conflicting["score"], 1.0)
        self.assertEqual(conflicting["confidence"], 0.9)
        self.assertEqual(conflicting["signal_count"], 2)
        self.assertEqual(conflicting["first_seen_at"], 200)
        self.assertEqual(conflicting["last_seen_at"], 200)

    def test_mode_match_scans_past_newer_incompatible_success(self) -> None:
        compatible = self.database.start_run(
            run_key="compatible-daily",
            report_date="2026-08-13",
            timezone="Europe/Amsterdam",
            model_id="model-a",
            prompt_version="v4",
            source_snapshot_hash="snapshot-a",
            config={
                "analysis_mode": "daily_current",
                "projection_version": "p2",
                "max_chunk_chars": 24000,
            },
            started_at=100,
        )
        self.database.finish_run(
            compatible["id"],
            status="success",
            report={
                "analysis_mode": "daily_current",
                "published": True,
                "model_status": "success",
            },
            finished_at=110,
        )
        newer = self.database.start_run(
            run_key="newer-incompatible-daily",
            report_date="2026-08-13",
            timezone="Europe/Amsterdam",
            model_id="model-b",
            prompt_version="v4",
            source_snapshot_hash="snapshot-a",
            config={"analysis_mode": "daily_current"},
            started_at=200,
        )
        self.database.finish_run(
            newer["id"],
            status="success",
            report={"analysis_mode": "daily_current", "published": True},
            finished_at=210,
        )

        match = self.database.matching_successful_report_for_mode(
            report_date="2026-08-13",
            analysis_mode="daily_current",
            timezone="Europe/Amsterdam",
            model_id="model-a",
            prompt_version="v4",
            source_snapshot_hash="snapshot-a",
            config_requirements={
                "projection_version": "p2",
                "max_chunk_chars": 24000,
            },
            report_requirements={"published": True},
        )

        self.assertEqual(match["id"], compatible["id"])

    def test_projection_reset_removes_only_selected_machine_rows(self) -> None:
        legacy = self.database.upsert_task_observation(
            {"task": {"task_key": "legacy", "title": "旧任务"}, "observed_at": 1}
        )
        current = self.database.upsert_task_observation(
            {
                "task": {
                    "task_key": "current",
                    "title": "新任务",
                    "payload": {"projection_version": "p2"},
                },
                "observed_at": 2,
            }
        )
        manual = self.database.upsert_task_observation(
            {"task": {"task_key": "manual", "title": "人工任务"}, "observed_at": 3}
        )
        self.database.set_task_status(manual["id"], "waiting", actor_kind="human")

        first = self.database.reset_machine_projections(projection_version="p2")
        self.assertEqual(first["tasks_archived"], 1)
        self.assertEqual(
            {item["task_key"] for item in self.database.list_tasks(limit=None)},
            {"current", "manual"},
        )
        second = self.database.reset_machine_projections(
            projection_version="p2", include_current=True
        )
        self.assertEqual(second["tasks_archived"], 1)
        self.assertEqual(
            {item["task_key"] for item in self.database.list_tasks(limit=None)},
            {"manual"},
        )

    def test_cached_run_projection_can_be_replayed_after_reset(self) -> None:
        run = self.database.start_run(run_key="replay-run")
        evidence = self.database.add_evidence(
            run["id"],
            {
                "evidence_key": "mail:1/a",
                "source_kind": "mail",
                "source_id": "a",
                "source_version": "v1",
                "content_text": "请跟进",
            },
        )
        self.database.upsert_task_observation(
            {
                "run_id": run["id"],
                "evidence_id": evidence["id"],
                "observation_key": "original-task-observation",
                "task": {
                    "task_key": "task:scoped",
                    "title": "跟进 A",
                    "payload": {"projection_version": "p2"},
                },
                "observed_status": "open",
                "observed_at": 10,
            }
        )
        self.database.upsert_opportunity_signal(
            {
                "run_id": run["id"],
                "evidence_id": evidence["id"],
                "signal_key": "original-opportunity-signal",
                "opportunity": {
                    "opportunity_key": "opportunity:scoped",
                    "title": "服务 A",
                    "payload": {"projection_version": "p2"},
                },
                "signal_kind": "qualification",
                "score": 0.6,
                "confidence": 0.8,
                "observed_at": 10,
            }
        )
        self.database.reset_machine_projections(
            projection_version="p2", include_current=True
        )
        self.assertEqual(self.database.list_tasks(limit=None), [])
        self.assertEqual(self.database.list_opportunities(), [])

        first = self.database.replay_run_projections(
            run["id"], campaign_id="campaign-2", projection_version="p2"
        )
        second = self.database.replay_run_projections(
            run["id"], campaign_id="campaign-2", projection_version="p2"
        )

        self.assertEqual(first["task_observations_replayed"], 1)
        self.assertEqual(second["opportunity_signals_replayed"], 1)
        self.assertEqual(self.database.list_tasks(limit=None)[0]["task_key"], "task:scoped")
        opportunity = self.database.list_opportunities()[0]
        self.assertEqual(opportunity["opportunity_key"], "opportunity:scoped")
        self.assertEqual(opportunity["signal_count"], 1)

        later_run = self.database.start_run(run_key="post-reset-run")
        self.database.upsert_opportunity_signal(
            {
                "run_id": later_run["id"],
                "signal_key": "post-reset-original-signal",
                "opportunity": {
                    "opportunity_key": "opportunity:scoped",
                    "title": "服务 A",
                    "payload": {"projection_version": "p2"},
                },
                "score": 0.7,
                "confidence": 0.8,
                "observed_at": 20,
            }
        )
        skipped = self.database.replay_run_projections(
            later_run["id"], campaign_id="campaign-2", projection_version="p2"
        )
        self.assertEqual(skipped["opportunity_signals_replayed"], 0)
        self.assertEqual(self.database.list_opportunities()[0]["signal_count"], 2)


if __name__ == "__main__":
    unittest.main()
