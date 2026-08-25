from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from feishu_archive.cli import (
    _LoadAwareBackfillClient,
    _warm_cold_start_vmlx,
    _run_insights_backfill_loop,
    _run_insights_backfill_step,
    _app_config,
    _client,
    _doctor,
    _mail_client,
    _mail_app_config,
    _mail_oauth_readiness,
    build_parser,
    main,
)
from feishu_archive.config import (
    ArchivePaths,
    DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOCAL_PORT,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_ERROR_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS,
    DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
    DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_BYTES,
    DEFAULT_SCOPES,
    MAIL_SCOPES,
    MAIL_TOKEN_NAMESPACE,
)
from feishu_archive.backfill import load_backfill_state
from feishu_archive.vmlx import VMLXResponseError
from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights import (
    PROJECTION_VERSION,
    PROMPT_VERSION,
    InsightsRunOptions,
    insights_run_identity,
)
from feishu_archive.insights_database import InsightsDatabase
from feishu_archive.insights_sources import calendar_day_window, extract_daily_sources
from feishu_archive.keychain import MemoryTokenStore
from feishu_archive.mail_database import MailDatabase


class AppConfigTests(unittest.TestCase):
    def test_doctor_requires_every_main_oauth_scope_and_fails_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            values = {
                "app_id": "cli_app",
                "cli_app:app_secret": "secret",
                "cli_app:refresh_token": "refresh",
                "cli_app:scope": " ".join(
                    scope
                    for scope in DEFAULT_SCOPES
                    if scope not in {"offline_access", "search:message"}
                ),
            }
            with (
                patch("feishu_archive.cli.KeychainStore") as store_class,
                patch("feishu_archive.cli.subprocess.run") as run,
                patch("feishu_archive.cli.shutil.disk_usage") as disk_usage,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                store_class.return_value.get.side_effect = values.get
                run.return_value = SimpleNamespace(stdout="FileVault is On.", stderr="")
                disk_usage.return_value = SimpleNamespace(free=20 * 1024**3)
                self.assertTrue(_doctor(database, paths.root))
            self.assertIn("飞书主 OAuth 权限", output.getvalue())
            self.assertIn("search:message", output.getvalue())

    def test_cold_start_warmup_initializes_idle_clock_then_defers(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.last_request_time = None
                self.chat_calls = 0

            def health(self):
                return {
                    "status": "healthy",
                    "model_loaded": True,
                    "model_name": "model",
                    "last_request_time": self.last_request_time,
                    "scheduler": {"num_running": 0, "num_waiting": 0},
                }

            def chat_json(self, messages, *, max_tokens, temperature):
                self.chat_calls += 1
                self.last_request_time = time.time()
                return {"ready": True}

        client = Client()
        missing = {
            "ready": False,
            "state": "unknown",
            "reason": "vmlx_last_request_uninitialized",
            "summary": {},
        }
        refreshed = _warm_cold_start_vmlx(
            client,
            missing,
            models=[{"id": "model"}],
            requested_model="model",
            minimum_idle_seconds=300,
        )

        self.assertEqual(client.chat_calls, 1)
        self.assertFalse(refreshed["ready"])
        self.assertEqual(refreshed["reason"], "vmlx_idle_cooldown")
        self.assertTrue(refreshed["summary"]["cold_start_warmup_attempted"])

        missing_field = {
            "ready": False,
            "state": "unknown",
            "reason": "vmlx_last_request_missing",
            "summary": {},
        }
        unchanged = _warm_cold_start_vmlx(
            client,
            missing_field,
            models=[{"id": "model"}],
            requested_model="model",
            minimum_idle_seconds=300,
        )
        self.assertIs(unchanged, missing_field)
        self.assertEqual(client.chat_calls, 1)

    def test_per_request_gate_can_self_prime_a_cold_engine(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.last_request_time = None
                self.calls = []

            def health(self):
                return {
                    "status": "healthy",
                    "model_loaded": True,
                    "model_name": "model",
                    "last_request_time": self.last_request_time,
                    "scheduler": {"num_running": 0, "num_waiting": 0},
                }

            def chat_json(self, messages, *, max_tokens, temperature):
                self.calls.append(messages)
                self.last_request_time = time.time()
                return {"ok": True}

        client = Client()
        gate = _LoadAwareBackfillClient(
            client,
            models=[{"id": "model"}],
            requested_model="model",
            maximum_wait_seconds=1,
            stability_seconds=0,
        )

        result = gate.chat_json(
            [{"role": "user", "content": "actual request"}],
            max_tokens=10,
            temperature=0.1,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][0]["content"], "actual request")

    def test_backfill_load_gate_latches_closed_after_health_failure(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.chat_calls = 0

            def health(self):
                raise RuntimeError("probe failed")

            def chat_json(self, messages, *, max_tokens, temperature):
                self.chat_calls += 1
                return {}

        client = Client()
        gate = _LoadAwareBackfillClient(
            client,
            models=[{"id": "model"}],
            requested_model="model",
            maximum_wait_seconds=0,
            poll_seconds=0.1,
        )
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "load_gate_closed"):
                gate.chat_json([], max_tokens=1, temperature=0.1)
        self.assertEqual(client.chat_calls, 0)

    def test_backfill_load_gate_stays_open_after_response_payload_error(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.chat_calls = 0

            def health(self):
                return {
                    "status": "healthy",
                    "model_loaded": True,
                    "model_name": "model",
                    "last_request_time": time.time(),
                    "scheduler": {"num_running": 0, "num_waiting": 0},
                }

            def chat_json(self, messages, *, max_tokens, temperature):
                self.chat_calls += 1
                if self.chat_calls == 1:
                    raise VMLXResponseError("vMLX response does not contain valid JSON")
                return {"ok": True}

        client = Client()
        gate = _LoadAwareBackfillClient(
            client,
            models=[{"id": "model"}],
            requested_model="model",
            stability_seconds=0,
        )
        with self.assertRaises(VMLXResponseError):
            gate.chat_json([], max_tokens=1, temperature=0.1)
        self.assertFalse(gate.blocked)
        result = gate.chat_json([], max_tokens=2, temperature=0.1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.chat_calls, 2)

    def test_backfill_load_gate_refuses_calls_past_window_budget(self) -> None:
        client = MagicMock()
        gate = _LoadAwareBackfillClient(
            client,
            models=[{"id": "model"}],
            requested_model="model",
            hard_deadline_monotonic=0,
        )
        with self.assertRaisesRegex(RuntimeError, "window_closed"):
            gate.chat_json([], max_tokens=1, temperature=0.1)
        self.assertTrue(gate.step_budget_exhausted)
        client.health.assert_not_called()
        client.chat_json.assert_not_called()

    def test_backfill_step_replays_cached_daily_and_resets_when_archive_empties(self) -> None:
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
            archive.upsert_conversation({"chat_id": "oc-backfill", "name": "回填群"})
            window = calendar_day_window("2026-08-12", "Europe/Amsterdam")
            occurred_at = int(window["start_ms"]) + 1000
            with archive.connection() as con:
                con.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, message_type, sender_id,
                        created_at, body_text, raw_json, archived_at
                    ) VALUES ('om-backfill', 'oc-backfill', 'text', 'other', ?,
                              '请跟进 A 项目', '{}', ?)
                    """,
                    (occurred_at, occurred_at),
                )

            source = extract_daily_sources(
                archive, mail, "2026-08-12", "Europe/Amsterdam"
            )
            historical_options = InsightsRunOptions(
                report_date="2026-08-12",
                analysis_mode="historical_backfill",
                activate=False,
                include_carryover=False,
            )
            identity = insights_run_identity(source, historical_options)
            daily_run = insights.start_run(
                run_key="compatible-daily-for-backfill",
                report_date="2026-08-12",
                timezone="Europe/Amsterdam",
                model_id=historical_options.model,
                prompt_version=PROMPT_VERSION,
                source_snapshot_hash=identity["source_snapshot_hash"],
                config={
                    "analysis_mode": "daily_current",
                    "max_chunk_chars": historical_options.max_chunk_chars,
                    "max_output_tokens": historical_options.max_output_tokens,
                    "projection_version": PROJECTION_VERSION,
                },
            )
            evidence_item = source["evidence"][0]
            evidence = insights.add_evidence(
                daily_run["id"],
                {
                    **evidence_item,
                    "evidence_key": evidence_item["evidence_id"],
                    "source_version": "v1",
                    "content_text": evidence_item["text"],
                },
            )
            insights.upsert_task_observation(
                {
                    "run_id": daily_run["id"],
                    "evidence_id": evidence["id"],
                    "observation_key": "daily-original-task",
                    "task": {
                        "task_key": "task:daily-scoped",
                        "title": "跟进 A 项目",
                        "payload": {"projection_version": PROJECTION_VERSION},
                    },
                    "observed_status": "open",
                    "observed_at": occurred_at,
                }
            )
            daily_report = {
                "report_date": "2026-08-12",
                "timezone": "Europe/Amsterdam",
                "model": historical_options.model,
                "prompt_version": PROMPT_VERSION,
                "analysis_mode": "daily_current",
                "model_status": "success",
                "published": True,
                "coverage": source["coverage"],
                "yesterday_summary": [],
                "today_plan": [],
                "commercial_opportunities": [],
            }
            insights.finish_run(
                daily_run["id"],
                status="success",
                report=daily_report,
                stats={"evidence": 1},
            )
            args = SimpleNamespace(
                timezone="Europe/Amsterdam",
                model=historical_options.model,
                start_hour=6,
                end_hour=22,
                minimum_idle_seconds=300,
                scheduled=False,
                host="192.168.100.179",
                user="apple",
                local_port=11435,
                remote_port=11435,
            )

            with patch("feishu_archive.cli._yesterday", return_value="2026-08-12"):
                _run_insights_backfill_step(args, paths, archive, mail)
                state = load_backfill_state(paths.insights_backfill_state)
                self.assertEqual(state["last_outcome"], "daily_current_covered")
                self.assertEqual(
                    insights.list_tasks(limit=None)[0]["task_key"],
                    "task:daily-scoped",
                )
                self.assertEqual(insights.status()["archived_tasks"], 1)

                _run_insights_backfill_step(args, paths, archive, mail)
                state = load_backfill_state(paths.insights_backfill_state)
                self.assertTrue(state["cumulative_ledger_complete"])
                original_campaign = state["campaign_id"]

                with archive.connection() as con:
                    con.execute(
                        "UPDATE messages SET deleted=1 WHERE message_id='om-backfill'"
                    )
                _run_insights_backfill_step(args, paths, archive, mail)

            state = load_backfill_state(paths.insights_backfill_state)
            self.assertNotEqual(state["campaign_id"], original_campaign)
            self.assertEqual(state["campaign_change_reason"], "source_snapshot_changed")
            self.assertEqual(state["last_report_date"], "2026-08-12")
            self.assertEqual(state["last_outcome"], "empty")
            self.assertFalse(state["cumulative_ledger_complete"])
            self.assertEqual(state["last_projection_reset"]["include_current"], 1)
            self.assertEqual(insights.list_tasks(limit=None), [])

    def test_backfill_parser_exposes_loop_mode_and_dedicated_tunnel_port(self) -> None:
        args = build_parser().parse_args(["insights-backfill-step"])
        self.assertFalse(args.loop)
        self.assertEqual(args.local_port, DEFAULT_INSIGHTS_BACKFILL_LOCAL_PORT)
        args = build_parser().parse_args(["insights-backfill-step", "--loop"])
        self.assertTrue(args.loop)

    def test_backfill_loop_continues_immediately_after_own_engine_step(self) -> None:
        outcomes = [
            {"outcome": "success", "reason": None, "last_outcome": "success"},
            {
                "outcome": "deferred",
                "reason": "scheduled_step_budget_exhausted",
                "last_outcome": None,
            },
            {"outcome": "success", "reason": None, "last_outcome": "empty"},
            {"outcome": "complete", "reason": None, "last_outcome": "audit_match"},
        ]
        calls: list[int | None] = []
        sleeps: list[float] = []

        def fake_step(args, paths, database, mail_database, *, min_idle_seconds=None):
            calls.append(min_idle_seconds)
            if not outcomes:
                raise SystemExit(0)
            return outcomes.pop(0)

        args = SimpleNamespace(scheduled=False)
        with patch(
            "feishu_archive.cli._run_insights_backfill_step", side_effect=fake_step
        ), patch("time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
            with self.assertRaises(SystemExit) as caught:
                _run_insights_backfill_loop(args, None, None, None)

        self.assertEqual(caught.exception.code, 0)
        self.assertTrue(args.scheduled)
        self.assertEqual(
            calls,
            [
                None,
                DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_CONTINUE_MIN_IDLE_SECONDS,
                None,
                None,
            ],
        )
        self.assertEqual(
            sleeps,
            [
                DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_LOOP_MONITOR_SECONDS,
            ],
        )

    def test_backfill_loop_waits_and_keeps_full_idle_after_external_activity(self) -> None:
        outcomes = [
            {"outcome": "deferred", "reason": "vmlx_scheduler_busy", "last_outcome": None},
            {"outcome": "deferred", "reason": "vmlx_idle_cooldown", "last_outcome": None},
            {"outcome": "audit_match", "reason": None, "last_outcome": "audit_match"},
        ]
        calls: list[int | None] = []
        sleeps: list[float] = []

        def fake_step(args, paths, database, mail_database, *, min_idle_seconds=None):
            calls.append(min_idle_seconds)
            if not outcomes:
                raise SystemExit(0)
            return outcomes.pop(0)

        with patch(
            "feishu_archive.cli._run_insights_backfill_step", side_effect=fake_step
        ), patch("time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
            with self.assertRaises(SystemExit) as caught:
                _run_insights_backfill_loop(SimpleNamespace(scheduled=False), None, None, None)

        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(calls, [None, None, None, None])
        self.assertEqual(
            sleeps,
            [
                DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_LOOP_POLL_SECONDS,
                DEFAULT_INSIGHTS_BACKFILL_LOOP_YIELD_SECONDS,
            ],
        )

    def test_backfill_loop_aborts_after_consecutive_errors(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def fake_step(args, paths, database, mail_database, *, min_idle_seconds=None):
            nonlocal calls
            calls += 1
            return {
                "outcome": "error",
                "reason": "analysis_failed:RuntimeError",
                "last_outcome": None,
            }

        with patch(
            "feishu_archive.cli._run_insights_backfill_step", side_effect=fake_step
        ), patch("time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
            with self.assertRaises(SystemExit) as caught:
                _run_insights_backfill_loop(SimpleNamespace(scheduled=False), None, None, None)

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(calls, DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS)
        self.assertEqual(
            sleeps,
            [DEFAULT_INSIGHTS_BACKFILL_LOOP_ERROR_SECONDS]
            * (DEFAULT_INSIGHTS_BACKFILL_LOOP_MAX_CONSECUTIVE_ERRORS - 1),
        )

    def test_mail_preflight_is_independent_of_archive_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.ArchiveDatabase"
        ) as archive_database:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--archive-dir", temp, "mail-preflight"])

            archive_database.assert_not_called()
            self.assertFalse((Path(temp) / "archive.sqlite3").exists())
            self.assertTrue((Path(temp) / "mail.sqlite3").exists())
            secret = Path(temp) / "reader.secret"
            self.assertTrue(secret.exists())
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertIn("schema/FTS", output.getvalue())

    def test_chat_command_does_not_initialize_mail_database(self) -> None:
        archive_database = MagicMock()
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.ArchiveDatabase", return_value=archive_database
        ), patch("feishu_archive.cli.MailDatabase") as mail_database, patch(
            "feishu_archive.cli.seed_demo",
            return_value={"conversations": 0, "messages_written": 0, "attachments": 0},
        ):
            main(["--archive-dir", temp, "demo"])

        archive_database.initialize.assert_called_once_with()
        mail_database.assert_not_called()

    def test_serve_degrades_mail_directory_failure_without_stopping_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mail").write_text("not a directory", encoding="utf-8")
            error = io.StringIO()
            with patch("feishu_archive.cli.serve") as serve_reader, contextlib.redirect_stderr(
                error
            ):
                main(["--archive-dir", temp, "serve"])

            self.assertTrue((root / "archive.sqlite3").exists())
            kwargs = serve_reader.call_args.kwargs
            self.assertIsNone(kwargs["mail_database"])
            self.assertIsNotNone(kwargs["mail_session_manager"])
            self.assertIsNone(kwargs["mail_sync_controller"])
            self.assertIn("FileExistsError", kwargs["mail_unavailable_reason"])
            self.assertIn("聊天与知识库阅读仍可用", error.getvalue())

    def test_serve_degrades_mail_database_initialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.MailDatabase"
        ) as mail_database, patch("feishu_archive.cli.serve") as serve_reader:
            mail_database.return_value.initialize.side_effect = RuntimeError("schema failed")
            main(["--archive-dir", temp, "serve"])

        kwargs = serve_reader.call_args.kwargs
        self.assertIsNone(kwargs["mail_database"])
        self.assertIn("schema failed", kwargs["mail_unavailable_reason"])

    def test_serve_degrades_invalid_reader_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reader.secret").write_text("short\n", encoding="utf-8")
            with patch("feishu_archive.cli.serve") as serve_reader:
                main(["--archive-dir", temp, "serve"])

        kwargs = serve_reader.call_args.kwargs
        self.assertIsNone(kwargs["mail_session_manager"])
        self.assertIn("解锁密钥无效", kwargs["mail_unavailable_reason"])

    def test_sync_defaults_to_all_history(self) -> None:
        args = build_parser().parse_args(["sync", "--all-discovered"])
        self.assertTrue(args.all_discovered)
        self.assertIsNone(args.days)

    def test_attachment_resume_defaults_to_four_workers(self) -> None:
        args = build_parser().parse_args(["attachments"])
        self.assertEqual(args.workers, 4)

    def test_scheduled_sync_uses_two_day_overlap(self) -> None:
        args = build_parser().parse_args(["scheduled-sync"])
        self.assertEqual(args.days, 2)

    def test_insights_commands_default_to_secure_daily_model_route(self) -> None:
        args = build_parser().parse_args(["insights-run"])
        self.assertEqual(args.timezone, "Europe/Amsterdam")
        self.assertEqual(args.host, "192.168.100.179")
        self.assertEqual(args.model, "vmlx/qwen3.8-27b-8bit")
        self.assertIsNone(args.identity_file)
        self.assertEqual(args.remote_port, 11435)
        self.assertFalse(args.no_model)
        self.assertFalse(args.dry_run)
        backfill = build_parser().parse_args(["insights-backfill-step", "--scheduled"])
        self.assertEqual(backfill.remote_port, 11435)
        self.assertEqual(backfill.minimum_idle_seconds, 60)
        self.assertEqual(backfill.maximum_step_seconds, 1800)
        self.assertEqual((backfill.start_hour, backfill.end_hour), (0, 24))
        self.assertTrue(backfill.scheduled)
        configure = build_parser().parse_args(
            ["insights-configure", "--bearer-token-stdin"]
        )
        self.assertTrue(configure.bearer_token_stdin)
        custom_identity = build_parser().parse_args(
            ["insights-run", "--identity-file", "/tmp/feishu-archive-key"]
        )
        self.assertEqual(custom_identity.identity_file, "/tmp/feishu-archive-key")

    def test_wiki_rebuild_can_force_local_rendering(self) -> None:
        args = build_parser().parse_args(["wiki-rebuild", "--force"])
        self.assertEqual(args.command, "wiki-rebuild")
        self.assertTrue(args.force)

    def test_mail_lane_has_independent_sync_and_reader_commands(self) -> None:
        sync_args = build_parser().parse_args(["mail-sync"])
        self.assertIsNone(sync_args.days)
        self.assertEqual(sync_args.max_pages, 5000)
        self.assertEqual(sync_args.max_mail_gib, DEFAULT_MAX_MAIL_BYTES / 1024**3)
        self.assertEqual(
            sync_args.max_attachment_mib,
            DEFAULT_MAX_MAIL_ATTACHMENT_BYTES / 1024**2,
        )
        self.assertEqual(sync_args.max_mail_gib, 10)
        self.assertEqual(sync_args.max_attachment_mib, 1024)
        self.assertIsNone(sync_args.folder)
        self.assertFalse(sync_args.skip_attachments)
        bounded_args = build_parser().parse_args(["mail-sync", "--days", "30"])
        self.assertEqual(bounded_args.days, 30)
        scoped_args = build_parser().parse_args(
            ["mail-sync", "--folder", "DRAFT", "--folder", "7421369296749756417"]
        )
        self.assertEqual(scoped_args.folder, ["DRAFT", "7421369296749756417"])
        scheduled_args = build_parser().parse_args(["mail-scheduled-sync"])
        self.assertEqual(scheduled_args.days, 2)
        self.assertEqual(scheduled_args.max_mail_gib, 10)
        self.assertEqual(scheduled_args.max_attachment_mib, 1024)
        reader_args = build_parser().parse_args(["mail-reader-url", "--open"])
        self.assertTrue(reader_args.open)
        permanent_args = build_parser().parse_args(["mail-reader-url", "--permanent"])
        self.assertTrue(permanent_args.permanent)
        self.assertFalse(permanent_args.lock)
        lock_args = build_parser().parse_args(["mail-reader-url", "--lock"])
        self.assertTrue(lock_args.lock)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["mail-reader-url", "--permanent", "--lock"]
            )

    def test_mail_reader_permanent_unlock_and_relock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = io.StringIO()
            with patch("feishu_archive.cli.webbrowser.open") as opener, contextlib.redirect_stdout(
                output
            ):
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--permanent",
                        "--open",
                    ]
                )

            marker = root / "mail-reader.always-unlocked"
            secret = root / "reader.secret"
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertTrue(secret.is_file())
            opened_url = opener.call_args.args[0]
            self.assertEqual(opened_url, "http://127.0.0.1:8765/?mode=mail")
            self.assertNotIn("#", output.getvalue())
            self.assertNotIn(secret.read_text(encoding="utf-8").strip(), output.getvalue())

            with patch("feishu_archive.cli.serve") as serve_reader:
                main(["--archive-dir", temp, "serve"])
            deployed_manager = serve_reader.call_args.kwargs["mail_session_manager"]
            self.assertIsNotNone(deployed_manager)
            self.assertTrue(deployed_manager.allows_request(None))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--archive-dir", temp, "mail-reader-url", "--lock"])
            self.assertFalse(marker.exists())
            self.assertTrue(secret.is_file())
            self.assertTrue((root / "mail-reader.policy-generation").is_file())
            self.assertIn("已恢复短期会话锁定", output.getvalue())

    def test_mail_reader_lock_rejects_open_without_changing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            main(["--archive-dir", temp, "mail-reader-url", "--permanent"])
            marker = Path(temp) / "mail-reader.always-unlocked"
            self.assertTrue(marker.exists())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--lock",
                        "--open",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertTrue(marker.exists())

    def test_invalid_reader_secret_does_not_enable_permanent_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reader.secret").write_text("short\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((root / "mail-reader.always-unlocked").exists())

    def test_nonloopback_permanent_unlock_does_not_create_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--host",
                        "0.0.0.0",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((Path(temp) / "mail-reader.always-unlocked").exists())

    def test_misresolved_localhost_does_not_create_permanent_policy(self) -> None:
        external_resolution = [
            (2, 1, 6, "", ("192.0.2.10", 0)),
        ]
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.web.socket.getaddrinfo",
            return_value=external_resolution,
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--host",
                        "localhost",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((Path(temp) / "mail-reader.always-unlocked").exists())

    def test_app_config_reads_credentials_from_keychain(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_keychain")
        store.set("cli_keychain:app_secret", "keychain-secret")

        with patch.dict(os.environ, {}, clear=True):
            config = _app_config(8877, store)

        self.assertEqual(config.app_id, "cli_keychain")
        self.assertEqual(config.app_secret, "keychain-secret")
        self.assertEqual(config.redirect_uri, "http://127.0.0.1:8877/oauth/callback")

    def test_environment_credentials_take_priority(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_keychain")
        store.set("cli_env:app_secret", "stored-secret")

        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "env-secret"},
            clear=True,
        ):
            config = _app_config(store=store)

        self.assertEqual(config.app_id, "cli_env")
        self.assertEqual(config.app_secret, "env-secret")

    def test_mail_app_can_be_separate_or_reuse_main_app_without_dropping_scopes(self) -> None:
        store = MemoryTokenStore()
        store.set("mail_app_id", "cli_mail")
        store.set("cli_mail:app_secret", "mail-secret")
        with patch.dict(os.environ, {}, clear=True):
            dedicated = _mail_app_config(8877, store)
        self.assertEqual(dedicated.app_id, "cli_mail")
        self.assertEqual(dedicated.scopes, MAIL_SCOPES)

        shared_store = MemoryTokenStore()
        shared_store.set("app_id", "cli_shared")
        shared_store.set("cli_shared:app_secret", "shared-secret")
        with patch.dict(os.environ, {}, clear=True):
            shared = _mail_app_config(8877, shared_store)
        self.assertEqual(shared.app_id, "cli_shared")
        self.assertEqual(set(shared.scopes), set(DEFAULT_SCOPES) | set(MAIL_SCOPES))

    def test_scheduled_mail_sync_requires_refresh_token_and_all_read_scopes(self) -> None:
        store = MemoryTokenStore()
        store.set("mail_app_id", "cli_mail")
        store.set("cli_mail:app_secret", "mail-secret")
        with patch.dict(os.environ, {}, clear=True):
            ready, detail = _mail_oauth_readiness(store)
        self.assertFalse(ready)
        self.assertIn("mail-auth", detail)

        store.set("cli_mail:mail:refresh_token", "refresh")
        store.set("cli_mail:mail:scope", " ".join(set(MAIL_SCOPES) - {"offline_access"}))
        with patch.dict(os.environ, {}, clear=True):
            ready, detail = _mail_oauth_readiness(store)
        self.assertTrue(ready)
        self.assertIn("cli_mail", detail)

    def test_mail_client_factory_always_uses_mail_token_namespace(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_shared")
        store.set("cli_shared:app_secret", "shared-secret")

        with patch.dict(os.environ, {}, clear=True), patch(
            "feishu_archive.cli.KeychainStore", return_value=store
        ):
            client = _mail_client()

        self.assertEqual(client.token_namespace, MAIL_TOKEN_NAMESPACE)
        self.assertEqual(client.account("refresh_token"), "cli_shared:mail:refresh_token")

        default_client = _client(store=store)
        self.assertEqual(default_client.config.app_id, client.config.app_id)
        self.assertEqual(default_client.account("refresh_token"), "cli_shared:refresh_token")
        self.assertIsNone(store.get("cli_shared:refresh_token"))


if __name__ == "__main__":
    unittest.main()
