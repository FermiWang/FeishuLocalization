from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights import (
    REDUCE_RETRY_USER_PROMPT,
    InsightsError,
    InsightsRunOptions,
    _due_at_ms,
    _merge_carryover_tasks,
    _persist_validated_observations,
    _reduce_failure_code,
    _validate_cached_observations,
    _validated_map,
    _validated_map_result,
    export_report,
    run_daily_insights,
    validate_report,
)
from feishu_archive.insights_database import InsightsDatabase
from feishu_archive.insights_sources import calendar_day_window
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


class MalformedReducerClient(FakeJSONClient):
    def chat_json(self, messages, *, max_tokens, temperature):
        if self.calls:
            self.calls += 1
            return {}
        return super().chat_json(
            messages, max_tokens=max_tokens, temperature=temperature
        )


class FlakyReducerClient(FakeJSONClient):
    """First Reduce reply is malformed; the corrective retry succeeds."""

    def __init__(self) -> None:
        super().__init__()
        self.reduce_attempts: list[list[dict]] = []

    def chat_json(self, messages, *, max_tokens, temperature):
        if self.calls:
            self.reduce_attempts.append([dict(message) for message in messages])
            self.calls += 1
            if len(self.reduce_attempts) == 1:
                return {}
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
    def test_cross_scope_observation_updates_each_container_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()

            first_run = database.start_run(run_key="scope-transition-open")
            first_source = {
                "evidence_id": "mail:1/a-open",
                "source_kind": "mail",
                "source_id": "a-open",
                "thread_key": "mail:thread:a",
                "occurred_at": 10,
                "text": "请提交资料",
                "metadata": {},
            }
            first_evidence = database.add_evidence(
                first_run["id"], {**first_source, "evidence_key": "mail:1/a-open"}
            )
            _persist_validated_observations(
                database,
                first_run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "提交资料",
                        "action": "提交资料",
                        "status": "open",
                        "evidence_ids": ["mail:1/a-open"],
                        "confidence": 0.9,
                    }
                ],
                {"mail:1/a-open": first_source},
                {"mail:1/a-open": first_evidence["id"]},
            )

            second_run = database.start_run(run_key="scope-transition-done")
            second_sources = {
                "mail:2/a-done": {
                    "evidence_id": "mail:2/a-done",
                    "source_kind": "mail",
                    "source_id": "a-done",
                    "thread_key": "mail:thread:a",
                    "occurred_at": 20,
                    "text": "A 线程确认完成",
                    "metadata": {},
                },
                "chat:2/b-done": {
                    "evidence_id": "chat:2/b-done",
                    "source_kind": "chat",
                    "source_id": "b-done",
                    "thread_key": "chat:thread:b",
                    "occurred_at": 21,
                    "text": "B 会话同步确认完成",
                    "metadata": {},
                },
            }
            second_evidence = {
                key: database.add_evidence(
                    second_run["id"], {**source, "evidence_key": key}
                )["id"]
                for key, source in second_sources.items()
            }
            _persist_validated_observations(
                database,
                second_run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "提交资料",
                        "action": "提交资料",
                        "status": "done",
                        "due_date": "2026-08-20",
                        "evidence_ids": list(second_sources),
                        "confidence": 0.95,
                    }
                ],
                second_sources,
                second_evidence,
                timezone="Europe/Amsterdam",
            )
            # Replaying the same validated model item is idempotent.
            _persist_validated_observations(
                database,
                second_run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "提交资料",
                        "action": "提交资料",
                        "status": "done",
                        "due_date": "2026-08-20",
                        "evidence_ids": list(second_sources),
                        "confidence": 0.95,
                    }
                ],
                second_sources,
                second_evidence,
                timezone="Europe/Amsterdam",
            )

            tasks = database.list_tasks(limit=None)
            self.assertEqual(len(tasks), 2)
            self.assertEqual({task["status"] for task in tasks}, {"done"})
            self.assertEqual(database.list_tasks(open_only=True, limit=None), [])
            expected_due_at = calendar_day_window(
                "2026-08-20", "Europe/Amsterdam"
            )["start_ms"]
            self.assertEqual({task["due_at"] for task in tasks}, {expected_due_at})
            self.assertEqual(
                {tuple(task["payload"]["source_scopes"]) for task in tasks},
                {("mail:thread:a",), ("chat:thread:b",)},
            )
            with database.connection() as con:
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM task_observations").fetchone()[0],
                    3,
                )

    def test_due_date_parser_rejects_ambiguous_model_text(self) -> None:
        expected = calendar_day_window("2026-08-20", "Europe/Amsterdam")[
            "start_ms"
        ]
        self.assertEqual(_due_at_ms("2026-08-20", "Europe/Amsterdam"), expected)
        self.assertEqual(_due_at_ms(1_787_177_600, "Europe/Amsterdam"), 1_787_177_600_000)
        self.assertIsNone(_due_at_ms("下周", "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(True, "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(0, "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(float("nan"), "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(float("inf"), "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(1.5, "Europe/Amsterdam"))
        self.assertIsNone(_due_at_ms(10**100, "Europe/Amsterdam"))

    def test_v3_projection_reuses_single_scope_manual_v2_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            legacy = database.upsert_task_observation(
                {
                    "task": {
                        "task_key": "task:v2-manual",
                        "title": "提交项目资料",
                        "project_key": "项目甲",
                        "owner_key": "负责人甲",
                        "payload": {
                            "projection_version": "chronological-v2",
                            "source_scopes": ["mail:thread:a"],
                        },
                    },
                    "observed_status": "open",
                    "observed_at": 10,
                }
            )
            database.set_task_status(
                legacy["id"], "done", actor_kind="human", occurred_at=15
            )
            database.reset_machine_projections(
                projection_version="chronological-v3", include_current=True
            )

            run = database.start_run(run_key="v3-manual-anchor")
            source = {
                "evidence_id": "mail:3/a",
                "source_kind": "mail",
                "source_id": "a",
                "thread_key": "mail:thread:a",
                "occurred_at": 20,
                "text": "模型再次识别为未完成",
                "metadata": {},
            }
            evidence = database.add_evidence(
                run["id"], {**source, "evidence_key": "mail:3/a"}
            )
            _persist_validated_observations(
                database,
                run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "提交项目资料",
                        "action": "提交项目资料",
                        "project": "项目甲",
                        "owner": "负责人甲",
                        "status": "open",
                        "evidence_ids": ["mail:3/a"],
                        "confidence": 0.9,
                    }
                ],
                {"mail:3/a": source},
                {"mail:3/a": evidence["id"]},
            )

            tasks = database.list_tasks(limit=None)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_key"], "task:v2-manual")
            self.assertEqual(tasks[0]["status"], "done")
            self.assertEqual(tasks[0]["status_source"], "manual")
            self.assertEqual(database.list_tasks(open_only=True, limit=None), [])
            with database.connection() as con:
                payload = json.loads(
                    con.execute(
                        "SELECT payload_json FROM task_observations "
                        "WHERE run_id=? ORDER BY id DESC LIMIT 1",
                        (run["id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(
                payload["manual_projection_anchor"]["task_key"],
                "task:v2-manual",
            )
            self.assertFalse(payload["manual_projection_anchor"]["requires_split"])

    def test_v3_projection_keeps_unique_multi_scope_manual_task_as_legacy_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            legacy = database.upsert_task_observation(
                {
                    "task": {
                        "task_key": "task:v2-multi-manual",
                        "title": "联合提交资料",
                        "payload": {
                            "projection_version": "chronological-v2",
                            "source_scopes": ["mail:thread:a", "chat:thread:b"],
                        },
                    },
                    "observed_status": "open",
                    "observed_at": 10,
                }
            )
            database.set_task_status(
                legacy["id"], "waiting", actor_kind="human", occurred_at=15
            )
            run = database.start_run(run_key="v3-multi-manual-anchor")
            sources = {
                "mail:4/a": {
                    "evidence_id": "mail:4/a",
                    "source_kind": "mail",
                    "source_id": "a",
                    "thread_key": "mail:thread:a",
                    "occurred_at": 20,
                    "text": "邮件观察",
                    "metadata": {},
                },
                "chat:4/b": {
                    "evidence_id": "chat:4/b",
                    "source_kind": "chat",
                    "source_id": "b",
                    "thread_key": "chat:thread:b",
                    "occurred_at": 21,
                    "text": "聊天观察",
                    "metadata": {},
                },
            }
            evidence = {
                key: database.add_evidence(
                    run["id"], {**source, "evidence_key": key}
                )["id"]
                for key, source in sources.items()
            }
            _persist_validated_observations(
                database,
                run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "联合提交资料",
                        "action": "联合提交资料",
                        "status": "open",
                        "evidence_ids": list(sources),
                        "confidence": 0.9,
                    }
                ],
                sources,
                evidence,
            )

            tasks = database.list_tasks(limit=None)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_key"], "task:v2-multi-manual")
            self.assertEqual(tasks[0]["status"], "waiting")
            with database.connection() as con:
                payloads = [
                    json.loads(row[0])
                    for row in con.execute(
                        "SELECT payload_json FROM task_observations "
                        "WHERE run_id=? ORDER BY id",
                        (run["id"],),
                    ).fetchall()
                ]
            self.assertEqual(len(payloads), 2)
            self.assertTrue(
                all(
                    payload["manual_projection_anchor"]["requires_split"]
                    for payload in payloads
                )
            )

    def test_v3_projection_reuses_manual_opportunity_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            legacy = database.upsert_opportunity_signal(
                {
                    "opportunity": {
                        "opportunity_key": "opportunity:v2-manual",
                        "entity_key": "客户甲",
                        "title": "尽调服务",
                        "payload": {
                            "projection_version": "chronological-v2",
                            "source_scopes": ["mail:thread:a"],
                        },
                    },
                    "signal_kind": "qualification",
                    "score": 0.6,
                    "confidence": 0.8,
                    "observed_at": 10,
                }
            )
            with database.connection() as con:
                con.execute(
                    "UPDATE opportunities SET status='dismissed', "
                    "status_source='manual' WHERE id=?",
                    (legacy["id"],),
                )
            run = database.start_run(run_key="v3-manual-opportunity-anchor")
            source = {
                "evidence_id": "mail:5/a",
                "source_kind": "mail",
                "source_id": "a",
                "thread_key": "mail:thread:a",
                "occurred_at": 20,
                "text": "出现新的尽调服务信号",
                "metadata": {},
            }
            evidence = database.add_evidence(
                run["id"], {**source, "evidence_key": "mail:5/a"}
            )
            _persist_validated_observations(
                database,
                run["id"],
                [
                    {
                        "kind": "opportunity_signals",
                        "summary": "尽调服务",
                        "need": "尽调服务",
                        "organization": "客户甲",
                        "strength": "qualification",
                        "evidence_ids": ["mail:5/a"],
                        "confidence": 0.9,
                    }
                ],
                {"mail:5/a": source},
                {"mail:5/a": evidence["id"]},
            )

            opportunities = database.list_opportunities()
            self.assertEqual(len(opportunities), 1)
            self.assertEqual(
                opportunities[0]["opportunity_key"], "opportunity:v2-manual"
            )
            self.assertEqual(opportunities[0]["status"], "dismissed")
            self.assertEqual(opportunities[0]["status_source"], "manual")
            self.assertEqual(opportunities[0]["signal_count"], 2)

    def test_ambiguous_manual_projection_anchors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = InsightsDatabase(Path(temp) / "insights.sqlite3")
            database.initialize()
            for index in (1, 2):
                task = database.upsert_task_observation(
                    {
                        "task": {
                            "task_key": f"task:manual-{index}",
                            "title": "提交资料",
                            "payload": {
                                "projection_version": "chronological-v2",
                                "source_scopes": ["mail:thread:a"],
                            },
                        },
                        "observed_status": "open",
                        "observed_at": index,
                    }
                )
                database.set_task_status(
                    task["id"], "done", actor_kind="human", occurred_at=10 + index
                )
            run = database.start_run(run_key="ambiguous-manual-anchor")
            source = {
                "evidence_id": "mail:6/a",
                "source_kind": "mail",
                "source_id": "a",
                "thread_key": "mail:thread:a",
                "occurred_at": 20,
                "text": "模型认为仍需提交",
                "metadata": {},
            }
            evidence = database.add_evidence(
                run["id"], {**source, "evidence_key": "mail:6/a"}
            )
            _persist_validated_observations(
                database,
                run["id"],
                [
                    {
                        "kind": "task_observations",
                        "summary": "提交资料",
                        "action": "提交资料",
                        "status": "open",
                        "evidence_ids": ["mail:6/a"],
                        "confidence": 0.9,
                    }
                ],
                {"mail:6/a": source},
                {"mail:6/a": evidence["id"]},
            )

            self.assertEqual(len(database.list_tasks(limit=None)), 2)
            self.assertEqual(database.list_tasks(open_only=True, limit=None), [])
            with database.connection() as con:
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM task_observations WHERE run_id=?",
                        (run["id"],),
                    ).fetchone()[0],
                    0,
                )

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

    def test_checkpoint_rejects_evidence_outside_its_chunk(self) -> None:
        allowed = {
            "chat:oc-1/om-1": {
                "source_kind": "chat",
                "metadata": {},
            }
        }
        cached = [
            {
                "kind": "facts",
                "summary": "跨分片伪引用",
                "evidence_ids": ["chat:oc-2/om-2"],
                "confidence": 0.9,
            }
        ]
        self.assertIsNone(_validate_cached_observations(cached, allowed))

    def test_map_requires_all_four_explicit_array_fields(self) -> None:
        evidence = {
            "chat:oc/om": {
                "source_kind": "chat",
                "metadata": {},
            }
        }
        malformed = (
            {},
            {"facts": []},
            {
                "facts": [],
                "decisions": [],
                "task_observations": [],
                "opportunity_signals": None,
            },
            {
                "facts": [],
                "decisions": [],
                "task_observations": [],
                "opportunity_signals": "",
            },
            {
                "facts": [],
                "decisions": [],
                "task_observations": [
                    {
                        "summary": "非标准截止日期",
                        "action": "提交资料",
                        "due_date": float("nan"),
                        "evidence_ids": ["chat:oc/om"],
                    }
                ],
                "opportunity_signals": [],
            },
        )
        for value in malformed:
            with self.subTest(value=value):
                self.assertEqual(_validated_map_result(value, evidence), ([], False))

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

    def test_malformed_reducer_is_partial_and_cannot_advance_backfill(self) -> None:
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
                    "INSERT INTO sync_jobs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    "INSERT INTO wiki_sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES ('om', 'oc', 'text', 'other', ?, '请跟进资料', '{}', ?)
                    """,
                    (1786485600001, 1786485600001),
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
                InsightsRunOptions(
                    report_date="2026-08-12",
                    analysis_mode="historical_backfill",
                    trigger="historical_backfill",
                    activate=False,
                    include_carryover=False,
                ),
                client=MalformedReducerClient(),
            )

            self.assertEqual(report["model_status"], "partial")
            self.assertFalse(report["published"])
            self.assertEqual(insights.status()["successful_runs"], 0)
            self.assertEqual(
                (report.get("reduce_failure") or {}).get("attempts"),
                ["reduce_missing_fields", "reduce_missing_fields"],
            )
            with insights.connection() as con:
                row = con.execute(
                    "SELECT error, stats_json FROM analysis_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertIn(
                "reduce_failure=reduce_missing_fields>reduce_missing_fields",
                row["error"],
            )
            stats = json.loads(row["stats_json"])
            self.assertEqual(
                stats["reduce_failure"],
                {
                    "attempts": ["reduce_missing_fields", "reduce_missing_fields"],
                    "retry_attempted": True,
                },
            )
            self.assertEqual(stats["reduce_retries"], 0)

    def test_reducer_retry_recovers_after_malformed_first_reply(self) -> None:
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
                    "INSERT INTO sync_jobs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    "INSERT INTO wiki_sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )
                con.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES ('om', 'oc', 'text', 'other', ?, '请跟进资料', '{}', ?)
                    """,
                    (1786485600001, 1786485600001),
                )
            with mail.connection() as con:
                con.execute(
                    "INSERT INTO sync_runs(trigger, started_at, finished_at, status) "
                    "VALUES ('test', 1, 2, 'success')"
                )

            client = FlakyReducerClient()
            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(
                    report_date="2026-08-12",
                    analysis_mode="historical_backfill",
                    trigger="historical_backfill",
                    activate=False,
                    include_carryover=False,
                ),
                client=client,
            )

            self.assertEqual(report["model_status"], "success")
            self.assertTrue(report["published"])
            self.assertEqual(report.get("reduce_retries"), 1)
            self.assertEqual(len(client.reduce_attempts), 2)
            self.assertEqual(
                client.reduce_attempts[1][-1]["content"], REDUCE_RETRY_USER_PROMPT
            )
            with insights.connection() as con:
                row = con.execute(
                    "SELECT stats_json FROM analysis_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(json.loads(row["stats_json"])["reduce_retries"], 1)

    def test_reduce_failure_code_is_metadata_only(self) -> None:
        from feishu_archive.vmlx import VMLXError

        self.assertEqual(
            _reduce_failure_code(VMLXError("vMLX request timed out")), "reduce_timeout"
        )
        self.assertEqual(
            _reduce_failure_code(VMLXError("vMLX response does not contain valid JSON")),
            "reduce_invalid_json",
        )
        self.assertEqual(
            _reduce_failure_code(
                VMLXError("vMLX response does not contain a single JSON object")
            ),
            "reduce_no_json_object",
        )
        self.assertEqual(
            _reduce_failure_code(InsightsError("Reducer 必须返回三个显式数组字段")),
            "reduce_missing_fields",
        )
        self.assertEqual(
            _reduce_failure_code(InsightsError("Reducer 字段 today_plan 引用了未见证据")),
            "reduce_unknown_evidence",
        )
        self.assertEqual(
            _reduce_failure_code(InsightsError("Reducer 字段 today_plan 引用了不可行动证据")),
            "reduce_inactionable_evidence",
        )
        self.assertEqual(
            _reduce_failure_code(RuntimeError("boom")), "reduce_request_failed"
        )

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
                            ],
                            "decisions": [],
                            "task_observations": [],
                            "opportunity_signals": [],
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

    def test_map_cannot_cite_evidence_from_an_unseen_chunk(self) -> None:
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

            class CrossChunkClient:
                def __init__(self) -> None:
                    self.maps = 0

                def chat_json(self, messages, *, max_tokens, temperature):
                    payload = json.loads(messages[-1]["content"])
                    if not isinstance(payload, dict):
                        raise AssertionError("没有有效观察时不应调用 Reduce")
                    self.maps += 1
                    allowed = set(payload["allowed_evidence_ids"])
                    foreign = (
                        "chat:oc-2/om-2"
                        if "chat:oc-2/om-2" not in allowed
                        else "chat:oc-1/om-1"
                    )
                    self.assert_foreign(foreign, allowed)
                    return {
                        "facts": [
                            {
                                "summary": "引用未见分片",
                                "evidence_ids": [foreign],
                                "confidence": 0.9,
                            }
                        ],
                        "decisions": [],
                        "task_observations": [],
                        "opportunity_signals": [],
                    }

                @staticmethod
                def assert_foreign(foreign, allowed):
                    if foreign in allowed:
                        raise AssertionError("测试证据必须来自另一个分片")

            client = CrossChunkClient()
            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(
                    report_date="2026-08-12",
                    max_chunk_chars=80,
                ),
                client=client,
            )

            self.assertEqual(client.maps, 2)
            self.assertEqual(report["validated_observations"], 0)
            self.assertEqual(report["model_status"], "partial")
            self.assertFalse(report["published"])
            self.assertEqual(insights.list_tasks(limit=None), [])

    def test_valid_empty_map_output_publishes_and_allows_backfill_progress(self) -> None:
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
            archive.upsert_conversation({"chat_id": "oc", "name": "通知群"})
            with archive.connection() as con:
                con.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES ('om', 'oc', 'text', 'other', ?,
                              '普通系统通知，无需提炼事项', '{}', ?)
                    """,
                    (1786485600001, 1786485600001),
                )

            class EmptyMapClient:
                def __init__(self) -> None:
                    self.calls = 0

                def chat_json(self, messages, *, max_tokens, temperature):
                    self.calls += 1
                    payload = json.loads(messages[-1]["content"])
                    if not isinstance(payload, dict):
                        raise AssertionError("合法空 Map 不需要调用 Reduce")
                    return {
                        "facts": [],
                        "decisions": [],
                        "task_observations": [],
                        "opportunity_signals": [],
                    }

            client = EmptyMapClient()
            report = run_daily_insights(
                archive,
                mail,
                insights,
                paths,
                InsightsRunOptions(
                    report_date="2026-08-12",
                    analysis_mode="historical_backfill",
                    trigger="historical_backfill",
                    activate=False,
                    include_carryover=False,
                ),
                client=client,
            )

            self.assertEqual(client.calls, 1)
            self.assertEqual(report["validated_observations"], 0)
            self.assertEqual(report["model_status"], "not_required")
            self.assertTrue(report["published"])
            self.assertFalse(report["degraded"])
            self.assertEqual(insights.status()["successful_runs"], 1)

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
