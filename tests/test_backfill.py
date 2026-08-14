from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from feishu_archive.backfill import (
    BackfillPolicy,
    backfill_window_remaining_seconds,
    ensure_backfill_state,
    evaluate_vmlx_load,
    load_backfill_state,
    mark_backfill_projection_initialized,
    record_backfill_audit,
    record_backfill_deferred,
    record_backfill_success,
    within_backfill_window,
)


class BackfillStateTests(unittest.TestCase):
    def test_forward_cursor_advances_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-11",
                timezone="Europe/Amsterdam",
                model="vmlx/gemma-4-31b-it-8bit",
                prompt_version="daily-insights-v3",
                analysis_config={"max_chunk_chars": 24000},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            self.assertEqual(state["next_date"], "2026-08-09")
            self.assertTrue(state["projection_reset_required"])
            state = mark_backfill_projection_initialized(path, state, now_ms=2)
            self.assertFalse(state["projection_reset_required"])
            deferred = record_backfill_deferred(
                path, state, reason="vmlx_scheduler_busy", now_ms=3
            )
            self.assertEqual(deferred["next_date"], "2026-08-09")
            self.assertEqual(deferred["processed_days"], 0)

            first = record_backfill_success(
                path,
                deferred,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09",
                run_id=7,
                empty_day=False,
                now_ms=4,
            )
            self.assertEqual(first["next_date"], "2026-08-10")
            self.assertEqual(first["processed_days"], 1)
            second = record_backfill_success(
                path,
                first,
                report_date="2026-08-10",
                source_snapshot_hash="snapshot-10",
                run_id=8,
                empty_day=True,
                now_ms=5,
            )
            final = record_backfill_success(
                path,
                second,
                report_date="2026-08-11",
                source_snapshot_hash="snapshot-11",
                run_id=9,
                empty_day=False,
                now_ms=6,
            )
            self.assertEqual(final["status"], "auditing")
            self.assertIsNone(final["next_date"])
            self.assertTrue(final["historical_backfill_complete"])
            self.assertTrue(final["historical_analysis_complete"])
            self.assertFalse(final["cumulative_ledger_complete"])
            self.assertEqual(final["reconciliation_status"], "audit_pending")
            self.assertEqual(final["audit_next_date"], "2026-08-09")
            self.assertEqual(final["empty_days"], 1)

            self.assertTrue(final["projection_initialized"])
            audited = record_backfill_audit(
                path,
                final,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09",
                now_ms=7,
            )
            audited = record_backfill_audit(
                path,
                audited,
                report_date="2026-08-10",
                source_snapshot_hash="snapshot-10",
                now_ms=8,
            )
            audited = record_backfill_audit(
                path,
                audited,
                report_date="2026-08-11",
                source_snapshot_hash="snapshot-11",
                now_ms=9,
            )
            self.assertEqual(audited["status"], "monitoring")
            self.assertTrue(audited["cumulative_ledger_complete"])
            self.assertEqual(audited["reconciliation_status"], "complete")
            self.assertEqual(audited["audit_cycles_completed"], 1)
            self.assertEqual(audited["audit_next_date"], "2026-08-09")
            self.assertEqual(load_backfill_state(path), audited)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_newer_bound_extends_without_restarting_forward_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            first = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-10",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            first = mark_backfill_projection_initialized(path, first, now_ms=2)
            progressed = record_backfill_success(
                path,
                first,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09",
                run_id=1,
                empty_day=False,
                now_ms=3,
            )
            expanded = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-11",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                extend_newest=True,
                now_ms=4,
            )
            self.assertEqual(expanded["campaign_id"], first["campaign_id"])
            self.assertEqual(expanded["newest_date"], "2026-08-11")
            self.assertEqual(expanded["next_date"], progressed["next_date"])
            changed = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-11",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 200},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=5,
            )
            self.assertNotEqual(changed["campaign_id"], expanded["campaign_id"])
            self.assertEqual(len(changed["previous_campaigns"]), 1)

    def test_completed_campaign_reopens_only_at_appended_upper_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-09",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            state = mark_backfill_projection_initialized(path, state, now_ms=2)
            complete = record_backfill_success(
                path,
                state,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09",
                run_id=1,
                empty_day=False,
                now_ms=3,
            )
            self.assertIsNone(complete["next_date"])
            appended = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-11",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                extend_newest=True,
                now_ms=4,
            )
            self.assertEqual(appended["campaign_id"], state["campaign_id"])
            self.assertEqual(appended["next_date"], "2026-08-10")
            self.assertEqual(appended["newest_date"], "2026-08-11")
            self.assertFalse(appended["cumulative_ledger_complete"])
            self.assertTrue(appended["projection_initialized"])
            self.assertFalse(appended["projection_reset_required"])
            self.assertIsNone(appended["audit_next_date"])
            self.assertEqual(appended["resume_audit_date"], "2026-08-09")

    def test_appended_upper_bound_resumes_audit_instead_of_restarting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-11",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            state = mark_backfill_projection_initialized(path, state, now_ms=2)
            for index, report_date in enumerate(
                ("2026-08-09", "2026-08-10", "2026-08-11"), 3
            ):
                state = record_backfill_success(
                    path,
                    state,
                    report_date=report_date,
                    source_snapshot_hash=f"snapshot-{report_date}",
                    run_id=index,
                    empty_day=False,
                    now_ms=index,
                )
            state = record_backfill_audit(
                path,
                state,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-2026-08-09",
                now_ms=7,
            )
            self.assertEqual(state["audit_next_date"], "2026-08-10")

            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-12",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                extend_newest=True,
                now_ms=8,
            )
            self.assertEqual(state["next_date"], "2026-08-12")
            self.assertEqual(state["resume_audit_date"], "2026-08-10")
            state = record_backfill_success(
                path,
                state,
                report_date="2026-08-12",
                source_snapshot_hash="snapshot-2026-08-12",
                run_id=12,
                empty_day=False,
                now_ms=9,
            )
            self.assertEqual(state["audit_next_date"], "2026-08-10")
            self.assertIsNone(state["resume_audit_date"])

    def test_changed_old_evidence_restarts_campaign_from_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-10",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            state = mark_backfill_projection_initialized(path, state, now_ms=2)
            state = record_backfill_success(
                path,
                state,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09-v1",
                run_id=1,
                empty_day=False,
                now_ms=3,
            )
            state = record_backfill_success(
                path,
                state,
                report_date="2026-08-10",
                source_snapshot_hash="snapshot-10-v1",
                run_id=2,
                empty_day=False,
                now_ms=4,
            )
            original_campaign = state["campaign_id"]

            restarted = record_backfill_audit(
                path,
                state,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-09-v2",
                now_ms=5,
            )

            self.assertNotEqual(restarted["campaign_id"], original_campaign)
            self.assertEqual(restarted["campaign_change_reason"], "source_snapshot_changed")
            self.assertEqual(restarted["next_date"], "2026-08-09")
            self.assertIsNone(restarted["audit_next_date"])
            self.assertFalse(restarted["projection_initialized"])
            self.assertTrue(restarted["projection_reset_required"])
            self.assertFalse(restarted["historical_analysis_complete"])
            self.assertFalse(restarted["cumulative_ledger_complete"])
            self.assertEqual(restarted["source_snapshot_hashes"], {})
            self.assertEqual(restarted["last_outcome"], "source_snapshot_changed")
            previous = restarted["previous_campaigns"][-1]
            self.assertEqual(previous["campaign_id"], original_campaign)
            self.assertEqual(previous["superseded_reason"], "source_snapshot_changed")
            self.assertEqual(
                previous["change_details"]["report_date"], "2026-08-09"
            )

    def test_audit_continues_in_strict_forward_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-10",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            state = mark_backfill_projection_initialized(path, state, now_ms=2)
            for index, report_date in enumerate(("2026-08-09", "2026-08-10"), 2):
                state = record_backfill_success(
                    path,
                    state,
                    report_date=report_date,
                    source_snapshot_hash=f"snapshot-{report_date}",
                    run_id=index,
                    empty_day=False,
                    now_ms=index,
                )
            for index, report_date in enumerate(("2026-08-09", "2026-08-10"), 5):
                state = record_backfill_audit(
                    path,
                    state,
                    report_date=report_date,
                    source_snapshot_hash=f"snapshot-{report_date}",
                    now_ms=index,
                )
            self.assertEqual(state["audit_next_date"], "2026-08-09")
            self.assertEqual(state["audit_cycles_completed"], 1)

            state = record_backfill_audit(
                path,
                state,
                report_date="2026-08-09",
                source_snapshot_hash="snapshot-2026-08-09",
                now_ms=7,
            )
            self.assertEqual(state["audit_next_date"], "2026-08-10")
            self.assertEqual(state["audit_cycles_completed"], 1)
            self.assertTrue(state["cumulative_ledger_complete"])

    def test_projection_must_be_initialized_before_analysis_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "insights" / "backfill-state.json"
            state = ensure_backfill_state(
                path,
                oldest_date="2026-08-09",
                newest_date="2026-08-10",
                timezone="Europe/Amsterdam",
                model="model",
                prompt_version="v1",
                analysis_config={"max_chunk_chars": 100},
                archive_bounds={"earliest_date": "2026-08-09"},
                now_ms=1,
            )
            with self.assertRaisesRegex(ValueError, "累计投影尚未"):
                record_backfill_success(
                    path,
                    state,
                    report_date="2026-08-09",
                    source_snapshot_hash="snapshot-09",
                    run_id=1,
                    empty_day=False,
                    now_ms=2,
                )
            initialized = mark_backfill_projection_initialized(path, state, now_ms=3)
            self.assertTrue(initialized["projection_initialized"])
            self.assertFalse(initialized["projection_reset_required"])
            self.assertEqual(initialized["next_date"], "2026-08-09")

    def test_scheduled_window_uses_explicit_timezone(self) -> None:
        policy = BackfillPolicy(
            timezone="Europe/Amsterdam",
            model="model",
            start_hour=6,
            end_hour=22,
            minimum_idle_seconds=300,
        )
        self.assertTrue(
            within_backfill_window(
                datetime(2026, 8, 14, 12, tzinfo=ZoneInfo("UTC")), policy
            )
        )
        self.assertFalse(
            within_backfill_window(
                datetime(2026, 8, 14, 3, tzinfo=ZoneInfo("UTC")), policy
            )
        )
        remaining = backfill_window_remaining_seconds(
            datetime(2026, 8, 14, 19, 50, tzinfo=ZoneInfo("UTC")), policy
        )
        self.assertEqual(remaining, 600)


class VMLXLoadGateTests(unittest.TestCase):
    def health(self, *, running=0, waiting=0, last_request=100.0):
        return {
            "status": "healthy",
            "model_loaded": True,
            "model_name": "model",
            "last_request_time": last_request,
            "scheduler": {"num_running": running, "num_waiting": waiting},
        }

    def test_requires_exact_model_zero_queue_and_idle_cooldown(self) -> None:
        models = [{"id": "model"}]
        idle = evaluate_vmlx_load(
            self.health(),
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertTrue(idle["ready"])
        self.assertEqual(idle["state"], "idle")

        busy = evaluate_vmlx_load(
            self.health(waiting=1),
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertFalse(busy["ready"])
        self.assertEqual(busy["state"], "busy")

        cooldown = evaluate_vmlx_load(
            self.health(last_request=450),
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertEqual(cooldown["state"], "cooldown")

        unknown = evaluate_vmlx_load(
            {**self.health(), "scheduler": {}},
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertEqual(unknown["state"], "unknown")

        cold_start = evaluate_vmlx_load(
            self.health(last_request=None),
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertEqual(cold_start["reason"], "vmlx_last_request_uninitialized")

        missing_field_health = self.health()
        del missing_field_health["last_request_time"]
        missing_field = evaluate_vmlx_load(
            missing_field_health,
            models,
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertEqual(missing_field["reason"], "vmlx_last_request_missing")

        for malformed in (True, "100", [], {}):
            invalid = evaluate_vmlx_load(
                self.health(last_request=malformed),
                models,
                requested_model="model",
                minimum_idle_seconds=300,
                now_seconds=500,
            )
            with self.subTest(last_request=malformed):
                self.assertEqual(invalid["reason"], "vmlx_last_request_invalid")

        mismatch = evaluate_vmlx_load(
            self.health(),
            [{"id": "other"}],
            requested_model="model",
            minimum_idle_seconds=300,
            now_seconds=500,
        )
        self.assertEqual(mismatch["state"], "unavailable")

    def test_extreme_scheduler_number_fails_closed(self) -> None:
        result = evaluate_vmlx_load(
            self.health(running=10**10000),
            [{"id": "model"}],
            requested_model="model",
            minimum_idle_seconds=0,
            now_seconds=500,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
