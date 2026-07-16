import tempfile
import unittest
from pathlib import Path

from feishu_archive.automation import SyncBusyError, acquire_sync_lock, run_sync_cycle
from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.sync import SyncCounts


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


if __name__ == "__main__":
    unittest.main()
