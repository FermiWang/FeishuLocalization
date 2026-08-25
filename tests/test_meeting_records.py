import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from feishu_archive.config import ArchivePaths
from feishu_archive.cli import main
from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights_database import InsightsDatabase
from feishu_archive.insights import InsightsRunOptions, run_daily_insights
from feishu_archive.insights_sources import extract_daily_sources
from feishu_archive.mail_database import MailDatabase
from feishu_archive.meeting_records_database import MeetingRecordsDatabase
from feishu_archive.meeting_records_sync import (
    MeetingRecordsSyncError,
    SSHMeetingRecordsExporter,
    sync_meeting_records,
)
from feishu_archive.reader_auth import ReaderSessionManager, enable_permanent_unlock
from feishu_archive.web import ArchiveHTTPServer


def _event(seq: int, *, revision: int = 1, content: str = "形成试点共识 [S001]") -> dict:
    structured = {
        "title": "消防系统健康监控升级会",
        "sections": [
            {
                "id": "consensus",
                "title": "会议共识",
                "kind": "table",
                "content": content,
                "source_refs": ["S001"],
            },
            {
                "id": "actions",
                "title": "行动安排",
                "kind": "table",
                "content": "开展 POC [S002]",
                "source_refs": ["S002"],
            },
        ],
    }
    payload = {
        "meeting_id": 7,
        "revision": revision,
        "meeting": {
            "title": "消防系统健康监控升级会",
            "meeting_date": "2026-08-25",
            "background": "头脑风暴",
        },
        "structured": structured,
        "model_id": "vmlx/qwen3.8-27b-8bit",
        "prompt_version": "detailed-meeting-record-v2",
        "editor_kind": "model" if revision == 1 else "user",
    }
    digest = hashlib.sha256(
        json.dumps(structured, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload["content_hash"] = digest
    return {
        "seq": seq,
        "meeting_id": 7,
        "event_type": "upsert",
        "revision": revision,
        "meeting_date": "2026-08-25",
        "payload": payload,
        "content_hash": digest,
    }


class _Exporter:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def fetch(self, after, limit):
        self.calls.append((after, limit))
        return self.pages.pop(0) if self.pages else {"cursor": after, "events": []}


class MeetingRecordsDatabaseTests(unittest.TestCase):
    def test_scheduled_insights_cannot_rewrite_a_stale_meeting_day(self):
        meeting_database = mock.Mock()
        meeting_database.stale_status.return_value = {"status": "pending"}
        options = InsightsRunOptions(
            report_date="2026-08-25",
            timezone="Asia/Shanghai",
            trigger="scheduled",
        )
        with self.assertRaisesRegex(ValueError, "只能由本人确认后刷新"):
            run_daily_insights(
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
                options,
                meeting_database=meeting_database,
            )

    def test_sync_command_keeps_meeting_database_independent_when_insights_is_absent(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "feishu_archive.cli.sync_meeting_records",
            return_value={"status": "success", "cursor": 0, "events_applied": 0},
        ), contextlib.redirect_stdout(io.StringIO()):
            main(["--archive-dir", temp, "meeting-records-sync"])
            self.assertTrue((Path(temp) / "meeting-records.sqlite3").is_file())
            self.assertFalse((Path(temp) / "insights.sqlite3").exists())

    def test_incremental_events_are_idempotent_chunked_and_mark_existing_report_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            database = MeetingRecordsDatabase(Path(temp) / "meeting-records.sqlite3")
            database.initialize()
            self.assertEqual(os.stat(database.path).st_mode & 0o777, 0o600)

            first = _event(1, content="甲" * 25_050)
            self.assertEqual(database.apply_events([first], report_dates=set()), 1)
            self.assertEqual(database.apply_events([first], report_dates=set()), 0)
            evidence = database.evidence_for_day("2026-08-25", "Asia/Shanghai")
            consensus = [item for item in evidence if ":consensus" in item["evidence_id"]]
            self.assertEqual(len(consensus), 3)
            self.assertTrue(all(len(item["text"]) < 10_100 for item in consensus))
            self.assertIn("会议共识（1/3）", consensus[0]["title"])
            self.assertEqual(database.stale_status("2026-08-25")["status"], "current")

            second = _event(2, revision=2, content="共识经人工修订 [S001]")
            self.assertEqual(
                database.apply_events([second], report_dates={"2026-08-25"}), 1
            )
            stale = database.stale_status("2026-08-25")
            self.assertEqual(stale["status"], "pending")
            self.assertEqual(stale["reason"], "会议证据已更新，待人工刷新")
            current = database.evidence_for_day("2026-08-25", "Asia/Shanghai")
            self.assertTrue(all(":r2:" in item["evidence_id"] for item in current))

            database.mark_refreshed("2026-08-25", 42)
            self.assertEqual(database.stale_status("2026-08-25")["status"], "current")

            deleted = {
                "seq": 3,
                "meeting_id": 7,
                "event_type": "delete",
                "revision": 2,
                "meeting_date": "2026-08-25",
                "payload": {"title": "消防系统健康监控升级会"},
                "content_hash": "d" * 64,
            }
            database.apply_events([deleted], report_dates={"2026-08-25"})
            self.assertEqual(database.evidence_for_day("2026-08-25", "Asia/Shanghai"), [])
            self.assertEqual(database.stale_status("2026-08-25")["status"], "pending")

            with sqlite3.connect(database.path) as connection:
                connection.execute("PRAGMA user_version=999")
            with self.assertRaisesRegex(RuntimeError, "高于程序支持"):
                database.initialize()

    def test_sync_updates_cursor_and_persists_failure_without_partial_cursor_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            database = MeetingRecordsDatabase(Path(temp) / "meeting-records.sqlite3")
            database.initialize()
            exporter = _Exporter([{"cursor": 1, "events": [_event(1)]}])
            result = sync_meeting_records(
                database, None, trigger="test", exporter=exporter, limit=200
            )
            self.assertEqual(result, {"status": "success", "cursor": 1, "events_applied": 1})
            self.assertEqual(database.cursor(), 1)

            class FailingExporter:
                def fetch(self, after, limit):
                    raise MeetingRecordsSyncError("remote unavailable")

            with self.assertRaises(MeetingRecordsSyncError):
                sync_meeting_records(
                    database, None, trigger="test-failure", exporter=FailingExporter()
                )
            status = database.status()
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["cursor"], 1)
            self.assertIn("remote unavailable", status["error"])

            gap_exporter = _Exporter([{"cursor": 3, "events": [_event(3)]}])
            with self.assertRaisesRegex(MeetingRecordsSyncError, "cursor is inconsistent"):
                sync_meeting_records(
                    database, None, trigger="test-gap", exporter=gap_exporter
                )
            self.assertEqual(database.cursor(), 1)

    def test_daily_sources_include_section_evidence_and_meeting_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = ArchiveDatabase(root / "archive.sqlite3")
            archive.initialize()
            mail = MailDatabase(root / "mail.sqlite3")
            mail.initialize()
            meetings = MeetingRecordsDatabase(root / "meeting-records.sqlite3")
            meetings.initialize()
            meetings.apply_events([_event(1, content="甲" * 25_050)], report_dates=set())
            run_id = meetings.start_sync("test")
            meetings.finish_sync(
                run_id, status="success", cursor=1, events_applied=1
            )
            source = extract_daily_sources(
                archive, mail, "2026-08-25", "Asia/Shanghai", meetings
            )
            counts = source["coverage"]["counts"]
            self.assertEqual(counts["meetings"], 1)
            self.assertEqual(counts["meeting_sections"], 2)
            evidence = [
                item for item in source["evidence"] if item["source_kind"] == "meeting"
            ]
            self.assertEqual(len(evidence), 4)
            self.assertIn("meeting:7:r1:actions", {
                item["evidence_id"] for item in evidence
            })
            self.assertEqual({
                (item["metadata"]["meeting_id"], item["metadata"]["section_id"])
                for item in evidence
            }, {(7, "consensus"), (7, "actions")})
            self.assertTrue(source["coverage"]["latest_sync"]["meeting"])

    def test_ssh_export_uses_validated_target_and_fixed_command(self):
        completed = mock.Mock(returncode=0, stdout='{"cursor":0,"events":[]}', stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            value = SSHMeetingRecordsExporter().fetch(0, 200)
        self.assertEqual(value["events"], [])
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1:3], ["-F", "/dev/null"])
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("apple@192.168.100.179", argv)
        self.assertEqual(
            argv[-1],
            "cd /Users/apple/meeting-minutes && .venv/bin/python3 -m app.export_events --after 0 --limit 200",
        )
        with self.assertRaises(ValueError):
            SSHMeetingRecordsExporter(host="192.168.100.179;touch /tmp/x")


class MeetingRefreshWebTests(unittest.TestCase):
    def test_refresh_requires_mail_session_confirmation_origin_and_pending_date(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            archive = ArchiveDatabase(paths.database)
            archive.initialize()
            mail = MailDatabase(paths.mail_database)
            mail.initialize()
            insights = InsightsDatabase(paths.insights_database)
            insights.initialize()
            meetings = MeetingRecordsDatabase(paths.meeting_records_database)
            meetings.initialize()
            meetings.apply_events([_event(1)], report_dates={"2026-08-25"})
            sessions = ReaderSessionManager(
                paths.reader_secret,
                permanent_unlock_path=paths.mail_reader_permanent_unlock,
            )
            enable_permanent_unlock(paths.mail_reader_permanent_unlock)
            controller = mock.Mock()
            controller.start.return_value = True
            controller.status.return_value = {"status": "idle"}
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0), archive, paths,
                mail_database=mail, mail_session_manager=sessions,
                insights_database=insights, meeting_records_database=meetings,
                insights_refresh_controller=controller,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                url = f"http://127.0.0.1:{port}/api/insights/refresh"
                body = b'{"report_date":"2026-08-25"}'
                missing = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(missing, timeout=2)
                self.assertEqual(context.exception.code, 403)

                evil = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Feishu-Archive-Action": "insights-refresh",
                        "Origin": f"http://attacker.example:{port}",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(evil, timeout=2)
                self.assertEqual(context.exception.code, 403)

                valid = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Feishu-Archive-Action": "insights-refresh",
                    },
                )
                with urllib.request.urlopen(valid, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                controller.start.assert_called_once_with("2026-08-25")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
