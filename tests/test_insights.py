from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights import (
    InsightsRunOptions,
    _merge_carryover_tasks,
    _persist_validated_observations,
    _validated_map,
    export_report,
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


class DynamicFakeJSONClient:
    def chat_json(self, messages, *, max_tokens, temperature):
        value = json.loads(messages[-1]["content"])
        if isinstance(value, dict) and "allowed_evidence_ids" in value:
            evidence_id = value["allowed_evidence_ids"][0]
            return {
                "facts": [],
                "decisions": [],
                "task_observations": [
                    {
                        "summary": "跟进该日资料",
                        "action": "跟进该日资料",
                        "status": "open",
                        "evidence_ids": [evidence_id],
                        "confidence": 0.9,
                    }
                ],
                "opportunity_signals": [],
            }
        evidence_id = value[0]["evidence_ids"][0]
        return {
            "yesterday_summary": [
                {
                    "summary": "该日资料已归档。",
                    "evidence_ids": [evidence_id],
                    "confidence": 0.9,
                }
            ],
            "today_plan": [
                {
                    "summary": "跟进该日资料",
                    "category": "committed",
                    "evidence_ids": [evidence_id],
                    "confidence": 0.9,
                }
            ],
            "commercial_opportunities": [],
        }


class TaskStatusClient:
    def __init__(self, status: str) -> None:
        self.status = status

    def chat_json(self, messages, *, max_tokens, temperature):
        value = json.loads(messages[-1]["content"])
        if isinstance(value, dict):
            evidence_id = value["allowed_evidence_ids"][0]
            return {
                "facts": [],
                "decisions": [],
                "task_observations": [
                    {
                        "summary": "跟进同一事项",
                        "action": "跟进同一事项",
                        "status": self.status,
                        "evidence_ids": [evidence_id],
                        "confidence": 0.9,
                    }
                ],
                "opportunity_signals": [],
            }
        evidence_id = value[0]["evidence_ids"][0]
        return {
            "yesterday_summary": [
                {
                    "summary": "事项状态已更新",
                    "evidence_ids": [evidence_id],
                    "confidence": 0.9,
                }
            ],
            "today_plan": [],
            "commercial_opportunities": [],
        }


class DailyInsightsTests(unittest.TestCase):
    def test_projection_identity_keeps_same_title_in_distinct_threads_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            run = database.start_run(run_key="scoped-tasks")
            sources = {
                "mail:1/a": {
                    "evidence_id": "mail:1/a",
                    "source_kind": "mail",
                    "source_id": "a",
                    "thread_key": "mail:1:thread:a",
                    "occurred_at": 10,
                    "text": "提交 A 资料",
                    "metadata": {},
                },
                "mail:1/b": {
                    "evidence_id": "mail:1/b",
                    "source_kind": "mail",
                    "source_id": "b",
                    "thread_key": "mail:1:thread:b",
                    "occurred_at": 20,
                    "text": "提交 B 资料",
                    "metadata": {},
                },
            }
            stored = {
                key: database.add_evidence(
                    run["id"], {**source, "evidence_key": key}
                )["id"]
                for key, source in sources.items()
            }
            observations = [
                {
                    "kind": "task_observations",
                    "summary": "提交资料",
                    "action": "提交资料",
                    "status": "open",
                    "evidence_ids": [key],
                    "confidence": 0.9,
                }
                for key in sources
            ]

            _persist_validated_observations(
                database, run["id"], observations, sources, stored
            )

            tasks = database.list_tasks(limit=None)
            self.assertEqual(len(tasks), 2)
            self.assertNotEqual(tasks[0]["task_key"], tasks[1]["task_key"])
            carryover = _merge_carryover_tasks([], tasks)
            self.assertEqual(len(carryover), 2)
            self.assertEqual({item["summary"] for item in carryover}, {"提交资料"})

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

    def test_historical_mode_does_not_activate_or_include_future_carryover(self) -> None:
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
                con.executemany(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES (?, 'oc', 'text', 'other', ?, '资料已发送，请跟进', '{}', ?)
                    """,
                    [
                        ("new", 1786485600001, 1786485600001),
                        ("old", 1786399200001, 1786399200001),
                    ],
                )

            current = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12"),
                client=DynamicFakeJSONClient(),
            )
            active_before = insights.latest_report()
            self.assertEqual(active_before["report_date"], "2026-08-12")
            insights.upsert_task_observation(
                {
                    "task": {"title": "只属于未来的累计待办"},
                    "observed_status": "open",
                    "observed_at": 1786485600001,
                }
            )

            historical = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(
                    report_date="2026-08-11",
                    trigger="historical_backfill",
                    analysis_mode="historical_backfill",
                    activate=False,
                    include_carryover=False,
                ),
                client=DynamicFakeJSONClient(),
            )
            self.assertTrue(historical["published"])
            self.assertEqual(historical["analysis_mode"], "historical_backfill")
            self.assertNotIn(
                "只属于未来的累计待办",
                {item["summary"] for item in historical["today_plan"]},
            )
            self.assertEqual(insights.latest_report()["id"], active_before["id"])
            json_path, markdown_path = export_report(paths, historical)
            self.assertEqual(json_path.parent.name, "history")
            self.assertTrue(markdown_path.is_file())
            self.assertTrue((paths.insights_exports / "2026-08-12.json").exists() is False)
            self.assertEqual(current["analysis_mode"], "daily_current")

    def test_historical_map_checkpoint_resumes_only_missing_chunks(self) -> None:
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
            for chat_id in ("oc-1", "oc-2"):
                archive.upsert_conversation({"chat_id": chat_id, "name": chat_id})
            with archive.connection() as con:
                con.executemany(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES (?, ?, 'text', 'other', ?, ?, '{}', ?)
                    """,
                    [
                        ("om-1", "oc-1", 1786485600001, "甲" * 60, 1786485600001),
                        ("om-2", "oc-2", 1786485600002, "乙" * 60, 1786485600002),
                    ],
                )

            checkpoint = paths.insights_backfill_checkpoints / "2026-08-12.json"
            options = InsightsRunOptions(
                report_date="2026-08-12",
                analysis_mode="historical_backfill",
                trigger="historical_backfill",
                activate=False,
                include_carryover=False,
                max_chunk_chars=80,
                map_checkpoint_path=checkpoint,
            )

            class Attempt:
                def __init__(self, *, fail_second_map: bool) -> None:
                    self.fail_second_map = fail_second_map
                    self.maps = 0

                def chat_json(self, messages, *, max_tokens, temperature):
                    payload = json.loads(messages[-1]["content"])
                    if isinstance(payload, dict):
                        self.maps += 1
                        if self.fail_second_map and self.maps == 2:
                            raise RuntimeError("transient failure")
                        evidence_id = payload["allowed_evidence_ids"][0]
                        return {
                            "facts": [
                                {
                                    "summary": "分片事实",
                                    "evidence_ids": [evidence_id],
                                    "confidence": 0.9,
                                }
                            ]
                        }
                    evidence_id = payload[0]["evidence_ids"][0]
                    return {
                        "yesterday_summary": [
                            {
                                "summary": "归纳结果",
                                "evidence_ids": [evidence_id],
                                "confidence": 0.9,
                            }
                        ],
                        "today_plan": [],
                        "commercial_opportunities": [],
                    }

            first_client = Attempt(fail_second_map=True)
            first = run_daily_insights(
                archive, mail, insights, paths, options, client=first_client
            )
            self.assertEqual(first["model_status"], "partial")
            self.assertEqual(first_client.maps, 2)
            self.assertTrue(checkpoint.is_file())

            resume_client = Attempt(fail_second_map=False)
            resumed = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                options,
                client=resume_client,
                now_ms=999999,
            )
            self.assertEqual(resumed["model_status"], "success")
            self.assertEqual(resume_client.maps, 1)
            self.assertFalse(checkpoint.exists())

    def test_current_run_done_status_does_not_merge_stale_open_carryover(self) -> None:
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
            archive.upsert_conversation({"chat_id": "oc-status", "name": "状态群"})
            with archive.connection() as con:
                con.executemany(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES (?, 'oc-status', 'text', 'other', ?, ?, '{}', ?)
                    """,
                    [
                        ("status-open", 1786399200001, "请跟进同一事项", 1786399200001),
                        ("status-done", 1786485600001, "同一事项已完成", 1786485600001),
                    ],
                )

            run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-11"),
                client=TaskStatusClient("open"),
            )
            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(report_date="2026-08-12"),
                client=TaskStatusClient("done"),
            )

            self.assertEqual(insights.list_tasks()[0]["status"], "done")
            self.assertNotIn(
                "跟进同一事项",
                {item["summary"] for item in report["today_plan"]},
            )


if __name__ == "__main__":
    unittest.main()
