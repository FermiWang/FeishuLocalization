import tempfile
import unittest
from pathlib import Path

from feishu_archive.automation import (
    SyncBusyError,
    acquire_mail_sync_lock,
    acquire_sync_lock,
    acquire_wiki_sync_lock,
    run_mail_sync_cycle,
    run_sync_cycle,
    run_wiki_sync_cycle,
)
from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.mail_database import MailDatabase
from feishu_archive.mail_sync import MailSyncCounts
from feishu_archive.sync import SyncCounts
from feishu_archive.wiki import WikiSyncCounts


class FakeSyncer:
    def __init__(self, database, calls):
        self.database = database
        self.calls = calls

    def discover(self):
        self.database.upsert_conversation(
            {"chat_id": "oc_new", "name": "新会话", "chat_mode": "p2p"}
        )
        return self.database.list_conversations()

    def sync(self, chat_ids, *, days=None):
        self.calls.append((list(chat_ids), days))
        counts = SyncCounts(messages_seen=len(chat_ids), messages_written=len(chat_ids))
        run_id = self.database.start_sync_run(list(chat_ids), days)
        self.database.finish_sync_run(run_id, status="success", **counts.as_dict())
        return counts


class FakeWikiSyncer:
    def __init__(self, database, client, paths, *, max_asset_bytes):
        self.calls = client

    def sync(self, space_ids, *, force=False):
        self.calls.append((space_ids, force))
        return WikiSyncCounts(spaces_seen=1, nodes_seen=4, documents_written=3), []


class FakeMailSyncer:
    def __init__(self, database, provider, paths, *, max_mail_bytes, max_attachment_bytes):
        self.database = database
        self.calls = provider

    def sync(self, *, folders, days, skip_attachments, trigger, max_pages):
        self.calls.append((folders, days, skip_attachments, trigger, max_pages))
        mailbox_id = self.database.upsert_mailbox(
            {"mailbox_id": "owner@example.com", "primary_email_address": "owner@example.com"}
        )
        run_id = self.database.start_sync_run(mailbox_id, trigger)
        counts = MailSyncCounts(messages_seen=2, messages_written=1)
        self.database.finish_sync_run(run_id, status="success", **counts.as_dict())
        return counts


class AutomationTests(unittest.TestCase):
    def test_cycle_full_syncs_new_chats_and_incrementally_syncs_existing_chats(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            database.upsert_conversation(
                {"chat_id": "oc_existing", "name": "已有会话", "chat_mode": "group"}
            )
            calls = []

            def syncer_factory(database, client, paths, *, max_attachment_bytes):
                return FakeSyncer(database, calls)

            result = run_sync_cycle(
                database,
                paths,
                lambda: object(),
                trigger="manual",
                overlap_days=2,
                syncer_factory=syncer_factory,
            )

            self.assertEqual(calls, [(["oc_new"], None), (["oc_existing"], 2)])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["trigger"], "manual")
            self.assertEqual(result["conversations_discovered"], 2)
            self.assertEqual(result["new_conversations"], 1)
            self.assertEqual(result["messages_seen"], 2)

    def test_sync_lock_rejects_a_second_task(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            lock = acquire_sync_lock(paths)
            try:
                with self.assertRaises(SyncBusyError):
                    acquire_sync_lock(paths)
            finally:
                lock.release()

    def test_wiki_cycle_has_independent_run_record_and_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            calls = []
            result = run_wiki_sync_cycle(
                database,
                paths,
                lambda: calls,
                trigger="manual",
                space_ids=["spc_1"],
                force=True,
                syncer_factory=FakeWikiSyncer,
            )
            self.assertEqual(calls, [(["spc_1"], True)])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["nodes_seen"], 4)

            message_lock = acquire_sync_lock(paths)
            wiki_lock = acquire_wiki_sync_lock(paths)
            try:
                with self.assertRaises(SyncBusyError):
                    acquire_wiki_sync_lock(paths)
            finally:
                wiki_lock.release()
                message_lock.release()

    def test_mail_cycle_has_independent_database_job_and_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = MailDatabase(paths.mail_database)
            database.initialize()
            calls = []
            result = run_mail_sync_cycle(
                database,
                paths,
                lambda: calls,
                trigger="manual",
                days=7,
                folders=["INBOX"],
                skip_attachments=True,
                max_pages=12,
                syncer_factory=FakeMailSyncer,
            )
            self.assertEqual(calls, [(["INBOX"], 7, True, "manual", 12)])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["messages_seen"], 2)

            message_lock = acquire_sync_lock(paths)
            wiki_lock = acquire_wiki_sync_lock(paths)
            mail_lock = acquire_mail_sync_lock(paths)
            try:
                with self.assertRaises(SyncBusyError):
                    acquire_mail_sync_lock(paths)
            finally:
                mail_lock.release()
                wiki_lock.release()
                message_lock.release()


if __name__ == "__main__":
    unittest.main()
