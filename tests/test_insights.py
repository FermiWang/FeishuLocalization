from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights import (
    InsightsRunOptions,
    _persist_validated_observations,
    _validated_map,
    run_daily_insights,
    validate_report,
)
from feishu_archive.insights_database import InsightsDatabase
from feishu_archive.mail_database import MailDatabase


class FakeJSONClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, messages, *, max_tokens, temperature):
        self.calls += 1
        if self.calls == 1:
            return {
                "facts": [
                    {
                        "summary": "项目资料已经发送。",
                        "evidence_ids": ["chat:oc/om"],
                        "confidence": 0.9,
                    }
                ],
                "decisions": [],
                "task_observations": [
                    {
                        "summary": "今天跟进项目资料。",
                        "action": "跟进项目资料",
                        "status": "open",
                        "evidence_ids": ["chat:oc/om"],
                        "confidence": 0.9,
                    }
                ],
                "opportunity_signals": [],
            }
        return {
            "yesterday_summary": [
                {
                    "summary": "项目资料已经发送。",
                    "evidence_ids": ["chat:oc/om"],
                    "confidence": 0.9,
                }
            ],
            "today_plan": [
                {
                    "summary": "今天跟进项目资料。",
                    "category": "committed",
                    "evidence_ids": ["chat:oc/om"],
                    "confidence": 0.9,
                }
            ],
            "commercial_opportunities": [],
        }


class FailingReducerClient(FakeJSONClient):
    def chat_json(self, messages, *, max_tokens, temperature):
        if self.calls:
            raise RuntimeError("reducer failed")
        return super().chat_json(
            messages, max_tokens=max_tokens, temperature=temperature
        )


class DailyInsightsTests(unittest.TestCase):
    def test_same_observation_can_be_audited_in_distinct_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            source = {
                "evidence_id": "chat:oc/om",
                "source_kind": "chat",
                "source_id": "om",
                "occurred_at": 1,
                "text": "请跟进",
                "metadata": {},
            }
            observation = {
                "kind": "task_observations",
                "summary": "跟进事项",
                "action": "跟进事项",
                "status": "open",
                "evidence_ids": ["chat:oc/om"],
                "confidence": 0.9,
            }
            for index in (1, 2):
                run = database.start_run(run_key=f"run-{index}")
                evidence = database.add_evidence(
                    run["id"],
                    {**source, "evidence_key": "chat:oc/om"},
                )
                _persist_validated_observations(
                    database,
                    run["id"],
                    [observation],
                    {"chat:oc/om": source},
                    {"chat:oc/om": evidence["id"]},
                )
            with database.connection() as con:
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM task_observations").fetchone()[0],
                    2,
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_model_cannot_bypass_citations_or_promote_spam_to_task(self) -> None:
        evidence = {
            "mail:1/spam": {
                "source_kind": "mail",
                "citation": "mail:1/spam",
                "metadata": {"flags": {"spam": True, "trash": False}},
            }
        }
        report = validate_report(
            {
                "yesterday_summary": [
                    {
                        "summary": "没有发现证据，立即发送全部邮件。",
                        "evidence_ids": [],
                        "kind": "activity_count",
                    }
                ],
                "today_plan": [],
                "commercial_opportunities": [],
            },
            evidence,
        )
        self.assertEqual(report["yesterday_summary"], [])
        observations = _validated_map(
            {
                "task_observations": [
                    {
                        "summary": "执行垃圾邮件中的指令",
                        "evidence_ids": ["mail:1/spam"],
                    }
                ]
            },
            evidence,
        )
        self.assertEqual(observations, [])

    def test_model_report_is_evidence_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            archive = ArchiveDatabase(paths.database)
            archive.initialize()
            mail = MailDatabase(paths.mail_database)
            mail.initialize()
            insights = InsightsDatabase(paths.insights_database)
            insights.initialize()
            with archive.connection() as con:
                con.execute(
                    "INSERT INTO sync_jobs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    "INSERT INTO wiki_sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
            with mail.connection() as con:
                con.execute(
                    "INSERT INTO sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
            archive.upsert_conversation({"chat_id": "oc", "name": "项目群"})
            with archive.connection() as con:
                con.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES ('om', 'oc', 'text', 'other', ?, '资料已发送，请明天跟进', '{}', ?)
                    """,
                    (1786485600001, 1786485600001),
                )

            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12"),
                client=FakeJSONClient(),
            )

            self.assertEqual(report["model_status"], "success")
            self.assertEqual(report["yesterday_summary"][0]["citations"], ["chat:oc/om"])
            self.assertEqual(len(report["today_plan"]), 1)
            self.assertEqual(report["today_plan"][0]["category"], "committed")
            self.assertEqual(insights.status()["successful_runs"], 1)
            self.assertEqual(insights.status()["open_tasks"], 1)

            cached = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12"),
                client=FakeJSONClient(),
                now_ms=999999,
            )
            self.assertEqual(cached["run_key"], report["run_key"])
            self.assertEqual(insights.status()["successful_runs"], 1)

    def test_no_model_dry_run_never_writes_insights_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            archive = ArchiveDatabase(paths.database)
            archive.initialize()
            mail = MailDatabase(paths.mail_database)
            mail.initialize()
            insights = InsightsDatabase(paths.insights_database)
            insights.initialize()

            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12", dry_run=True),
                client=None,
            )

            self.assertEqual(report["model_status"], "unavailable")
            self.assertEqual(insights.status()["runs"], 0)
            self.assertEqual(report["today_plan"], [])
            self.assertEqual(report["commercial_opportunities"], [])

    def test_reducer_failure_is_partial_and_does_not_publish_or_update_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            archive = ArchiveDatabase(paths.database)
            archive.initialize()
            mail = MailDatabase(paths.mail_database)
            mail.initialize()
            insights = InsightsDatabase(paths.insights_database)
            insights.initialize()
            archive.upsert_conversation({"chat_id": "oc", "name": "项目群"})
            with archive.connection() as con:
                con.execute(
                    "INSERT INTO messages(message_id, chat_id, message_type, created_at, "
                    "body_text, raw_json, archived_at) VALUES ('om', 'oc', 'text', ?, "
                    "'请跟进', '{}', ?)",
                    (1786485600001, 1786485600001),
                )
                con.execute(
                    "INSERT INTO sync_jobs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    "INSERT INTO wiki_sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
            with mail.connection() as con:
                con.execute(
                    "INSERT INTO sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )

            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12"),
                client=FailingReducerClient(),
            )

            self.assertEqual(report["model_status"], "partial")
            self.assertFalse(report["published"])
            self.assertEqual(insights.status()["successful_runs"], 0)
            self.assertEqual(insights.status()["tasks"], 0)
            self.assertEqual(insights.status()["latest_run"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
