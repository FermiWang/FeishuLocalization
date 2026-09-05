"""Background snapshots stay contextual, cached and separate from speech evidence."""
import copy
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MEETING_MINUTES_DATA_DIR", tempfile.mkdtemp(prefix="meeting-integration-suite-"))

from app import background, db, llm, processor
from app.main import app
from app.sources import input_hash


@pytest.fixture
def meeting_db():
    with tempfile.TemporaryDirectory(prefix="meeting-background-test-") as folder:
        root = Path(folder)
        with mock.patch.multiple(db, DATA_DIR=root, UPLOAD_DIR=root / "uploads",
                                 EXPORT_DIR=root / "exports", DB_PATH=root / "meetings.db",
                                 _local=threading.local()):
            db.init_db()
            yield
            db.get_conn().close()


def snapshot(url="https://example.com/event"):
    return {"url": url, "final_url": url, "title": "活动与讲者介绍", "text": "网页列出讲者 A；网页内容仅为背景。",
            "content_hash": "page-hash", "fetched_at": "2026-09-05T10:00:00+00:00", "truncated": False}


def test_background_cached_on_resume_and_invalidated_on_edit(meeting_db):
    mid = db.create_meeting("会议", "2026-09-02", "介绍 https://example.com/event#intro", [])
    first = db.get_meeting(mid)
    original_hash = input_hash(first)
    with mock.patch.object(background, "fetch_background", return_value=snapshot()) as fetch:
        prepared = processor.prepare_background(first)
        assert prepared["background_pages"][0]["url"].endswith("#intro")
        assert input_hash(prepared) != original_hash
        assert processor.prepare_background(prepared)["background_pages"] == prepared["background_pages"]
        assert fetch.call_count == 1
    db.update_meeting(mid, title="改标题", meeting_date="2026-09-02", background=prepared["background"])
    assert db.get_meeting(mid)["background_pages"]
    db.update_meeting(mid, title="改标题", meeting_date="2026-09-02", background="新背景")
    assert db.get_meeting(mid)["background_pages"] == []
    assert not db.save_background_pages(mid, prepared["background"], [snapshot()])


def test_failed_background_is_visible_cached_and_does_not_block_text(meeting_db):
    mid = db.create_meeting("会议", "2026-09-02", "https://example.com/event", [])
    with mock.patch.object(background, "fetch_background", side_effect=background.BackgroundFetchError("HTTP 403")) as fetch:
        result = processor.prepare_background(db.get_meeting(mid))
        again = processor.prepare_background(result)
        assert fetch.call_count == 1
        assert again["background_pages"][0]["error"] == "HTTP 403"
    with mock.patch.object(background, "fetch_background", return_value=snapshot()):
        refreshed = processor.prepare_background(again, force=True)
        assert refreshed["background_pages"][0]["status"] == "ready"


def test_refresh_api_protected_and_does_not_create_record_or_sync_event(meeting_db):
    mid = db.create_meeting("会议", "2026-09-02", "https://example.com/event", [])
    client = TestClient(app)  # No lifespan: no processing workers in this test.
    path = f"/api/meetings/{mid}/background/refresh"
    with mock.patch.object(background, "fetch_background", return_value=snapshot()) as fetch:
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"X-Meeting-Minutes-Action": "confirm", "Origin": "https://evil.example"}).status_code == 403
        assert fetch.call_count == 0
        result = client.post(path, headers={"X-Meeting-Minutes-Action": "confirm"})
        assert result.status_code == 200
        assert result.json()["items"][0]["status"] == "ready"
    assert db.get_record(mid) is None
    assert not db.export_events(0)["events"]
    assert client.get(f"/api/meetings/{mid}").json()["background_pages"][0]["text"]


def test_refresh_never_overwrites_newer_background(meeting_db):
    mid = db.create_meeting("会议", "2026-09-02", "https://example.com/event", [])
    old = db.get_meeting(mid)
    def fetch(url):
        db.update_meeting(mid, title="会议", meeting_date="2026-09-02", background="已修改")
        return snapshot(url)
    with mock.patch.object(background, "fetch_background", side_effect=fetch):
        with pytest.raises(ValueError, match="背景已修改"):
            processor.prepare_background(old)
    assert db.get_meeting(mid)["background_pages"] == []


def test_new_context_reaches_final_model_but_not_speech_extraction():
    page = {**snapshot(), "status": "ready"}
    meeting = {"title": "会议", "meeting_date": "2026-09-02", "background": page["url"], "background_pages": [page]}
    fragments = [{"id": "S001", "source_id": 1, "text": "真实发言", "speaker_labeled": False}]
    payloads = []
    def final_batch(specs, payload, refs, cache, **kwargs):
        payloads.append(copy.deepcopy(payload))
        return [{"id": sid, "title": title, "kind": kind, "content": "真实发言 [S001]", "source_refs": ["S001"]}
                for sid, title, kind in specs], []
    extracted = {"topics": [], "items": [{"kind": "meeting_fact", "text": "真实发言", "source_refs": ["S001"]}]}
    with mock.patch.object(llm, "model_preflight"), mock.patch.object(llm, "_chat_json", return_value=extracted) as extract, mock.patch.object(llm, "_generate_sections_adaptive", side_effect=final_batch):
        result = llm.organize(meeting, fragments)
    assert "网页列出讲者" not in json.dumps(extract.call_args.args, ensure_ascii=False)
    assert payloads[0]["background_context"][0]["text"] == page["text"]
    assert payloads[0]["valid_source_refs"] == ["S001"]
    assert "非会议发言证据" in result["sections"][1]["content"]
    assert result["provenance"]["background_sources"][0]["content_hash"] == "page-hash"
    assert page["text"] not in json.dumps(result["provenance"], ensure_ascii=False)


def test_worker_retains_srt_and_background_provenance_in_revision_and_export(meeting_db):
    mid = db.create_meeting("会议", "2026-09-02", "https://example.com/event", [])
    text = "[00:00:01.000 --> 00:00:02.000]\n真实发言。"
    db.add_source(mid, source_type="transcript", original_name="event.srt", stored_path="",
                  sha256=hashlib.sha256(text.encode()).hexdigest(), text_content=text)
    job = db.enqueue_job(mid, input_hash(db.get_meeting(mid)))
    claimed = db.claim_next_job()
    seen = []
    def organize(meeting, fragments, **kwargs):
        seen.append((meeting, fragments))
        return llm._normalize_final({"sections": [
            {"id": sid, "title": title, "kind": kind, "content": "真实发言 [S001]", "source_refs": ["S001"]}
            for sid, title, kind in llm.REQUIRED_SECTIONS]}, meeting, {"S001"})
    with mock.patch.object(background, "fetch_background", return_value=snapshot()), mock.patch.object(llm, "organize", side_effect=organize), mock.patch.object(processor, "build_docx"):
        processor.process_job(claimed)
    assert db.get_job(job["id"])["status"] == "done"
    assert seen[0][0]["source_policy"]["subtitle_timestamps_preserved"]
    assert "00:00:01.000" in seen[0][1][0]["text"]
    revision = db.get_record(mid)
    assert revision["source_fragments"][0]["text"] == text
    assert revision["structured"]["provenance"]["background_sources"][0]["content_hash"] == "page-hash"
    exported = db.export_events(0)["events"][0]
    assert exported["payload"]["structured"]["provenance"]["background_sources"]
    assert "source_fragments" not in exported["payload"]
    assert snapshot()["text"] not in json.dumps(exported, ensure_ascii=False)


def test_migration_adds_background_column_without_losing_old_data(meeting_db):
    con = db.get_conn()
    mid = db.create_meeting("旧会议", "2026-08-25", "旧背景", [])
    con.execute("ALTER TABLE meetings DROP COLUMN background_pages_json")
    con.execute("PRAGMA user_version=4")
    con.commit()
    db.init_db()
    migrated = db.get_meeting(mid)
    assert migrated["background"] == "旧背景"
    assert migrated["background_pages"] == []
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5
