import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx
from docx import Document
from fastapi.testclient import TestClient


_DATA_DIR = Path(tempfile.mkdtemp(prefix="meeting-minutes-tests-"))
os.environ["MEETING_MINUTES_DATA_DIR"] = str(_DATA_DIR)

from app import db, llm, processor  # noqa: E402
from app.docx_export import build_docx  # noqa: E402
from app.main import app  # noqa: E402
from app.sources import extract_transcript, input_hash, stable_fragments  # noqa: E402


def _structured(title="测试会议"):
    return {
        "title": title,
        "subtitle": "详细会议记录",
        "meeting_meta": {
            "title": title,
            "meeting_date": "2026-08-25",
            "background": "测试",
            "attendees": [{"name": "说话人1", "role": "主持"}],
        },
        "sections": [
            {
                "id": section_id,
                "title": section_title,
                "kind": kind,
                "content": f"{section_title}内容 [S001]",
                "source_refs": ["S001"],
            }
            for section_id, section_title, kind in llm.REQUIRED_SECTIONS
        ],
        "provenance": {
            "model_id": llm.EXACT_MODEL_ID,
            "prompt_version": llm.PROMPT_VERSION,
        },
    }


class DetailedRecordDatabaseTests(unittest.TestCase):
    def setUp(self):
        db.init_db()
        con = db.get_conn()
        for table in (
            "sync_events", "record_revisions", "processing_jobs", "meeting_sources",
            "minutes", "attendees", "meetings",
        ):
            con.execute(f"DELETE FROM {table}")
        con.commit()

    def test_multi_source_order_pair_revision_conflict_and_append_only_events(self):
        meeting_id = db.create_meeting("测试会议", "2026-08-25", "背景", [])
        first_path = db.UPLOAD_DIR / "first.txt"
        second_path = db.UPLOAD_DIR / "second.txt"
        first_path.write_text("第一段", encoding="utf-8")
        second_path.write_text("第二段", encoding="utf-8")
        digest = hashlib.sha256("同一哈希".encode()).hexdigest()
        first = db.add_source(
            meeting_id, source_type="transcript", original_name="first.txt",
            stored_path=str(first_path), sha256=digest, text_content="第一段",
        )
        second = db.add_source(
            meeting_id, source_type="transcript", original_name="second.txt",
            stored_path=str(second_path), sha256=digest, text_content="第二段",
        )
        db.reorder_sources(meeting_id, [second["id"], first["id"]])
        db.set_source_pair(meeting_id, second["id"], "pair-A")
        ordered = db.list_sources(meeting_id)
        self.assertEqual([item["id"] for item in ordered], [second["id"], first["id"]])
        self.assertEqual(ordered[0]["pair_key"], "pair-A")

        meeting = db.get_meeting(meeting_id)
        saved = db.save_revision(
            meeting_id, _structured(), "# 测试会议\n", input_hash=input_hash(meeting),
            model_id=llm.EXACT_MODEL_ID, prompt_version=llm.PROMPT_VERSION,
            editor_kind="model", base_revision=0,
            source_fragments=[{
                "id": "S001", "source_id": first["id"],
                "text": "第一段", "speaker_labeled": False,
            }],
        )
        self.assertEqual(saved["revision"], 1)
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            db.save_revision(
                meeting_id, _structured(), "# 冲突\n", input_hash=input_hash(meeting),
                model_id=llm.EXACT_MODEL_ID, prompt_version=llm.PROMPT_VERSION,
                editor_kind="user", base_revision=0,
            )
        second_revision = db.save_revision(
            meeting_id, _structured(), "# 修订\n", input_hash=input_hash(meeting),
            model_id=llm.EXACT_MODEL_ID, prompt_version=llm.PROMPT_VERSION,
            editor_kind="user", base_revision=1,
        )
        self.assertEqual(second_revision["revision"], 2)
        self.assertEqual(second_revision["source_fragments"][0]["id"], "S001")
        events = db.export_events(0)["events"]
        self.assertEqual([item["revision"] for item in events], [1, 2])
        self.assertNotIn("stored_path", json.dumps(events, ensure_ascii=False))
        self.assertNotIn("第一段", json.dumps(events, ensure_ascii=False))

        files = db.delete_meeting(meeting_id)
        self.assertIn(str(first_path), files)
        tombstone = db.export_events(events[-1]["seq"])["events"]
        self.assertEqual(tombstone[0]["event_type"], "delete")

    def test_job_recovery_and_input_hash_reset(self):
        meeting_id = db.create_meeting("测试会议", "2026-08-25", "", [])
        path = db.UPLOAD_DIR / "input.txt"
        path.write_text("内容", encoding="utf-8")
        db.add_source(
            meeting_id, source_type="transcript", original_name="input.txt",
            stored_path=str(path), sha256=db.file_sha256(path), text_content="内容",
        )
        meeting = db.get_meeting(meeting_id)
        job = db.enqueue_job(meeting_id, input_hash(meeting))
        claimed = db.claim_next_job()
        self.assertEqual(claimed["id"], job["id"])
        db.update_job(job["id"], checkpoint={"extracted": [{"x": 1}]})
        self.assertEqual(db.recover_jobs(), 1)
        db.reset_job_input(job["id"], "new-hash", status="queued")
        reset = db.get_job(job["id"])
        self.assertEqual(reset["input_hash"], "new-hash")
        self.assertEqual(json.loads(reset["checkpoint_json"]), {})

    def test_completed_legacy_six_section_result_migrates_without_rewriting_original(self):
        meeting_id = db.create_meeting("历史会议", "2026-08-20", "旧数据", [])
        original = (
            "## 会议纪要\n讨论消防水压。\n\n"
            "## 会议达成一致\n先开展试点。\n\n"
            "## 会议未达成一致\n传感器型号待定。\n\n"
            "## 会议待办\n- [ ] 完成 POC"
        )
        db.set_minutes_status(meeting_id, "done", content=original)
        con = db.get_conn()
        con.execute("DELETE FROM schema_meta WHERE key='legacy_minutes_v1'")
        con.commit()
        db._migrate_legacy_minutes()
        con.commit()
        record = db.get_record(meeting_id)
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["markdown"], original)
        self.assertEqual(record["editor_kind"], "legacy-migration")
        self.assertIn(
            "不补造证据引用",
            next(
                section["content"] for section in record["structured"]["sections"]
                if section["id"] == "compilation-notes"
            ),
        )
        self.assertEqual(db.export_events(0)["events"][0]["event_type"], "upsert")

        interrupted_id = db.create_meeting("中断会议", "2026-08-20", "", [])
        db.set_minutes_status(interrupted_id, "processing")
        db._recover_legacy_processing_state()
        con.commit()
        interrupted = db.get_minutes(interrupted_id)
        self.assertEqual(interrupted["status"], "pending")
        self.assertIn("重新开始整理", interrupted["error"])


class SourceAndDocumentTests(unittest.TestCase):
    def test_docx_and_txt_are_extracted_and_fragments_have_stable_ids(self):
        source = _DATA_DIR / "识别稿.docx"
        document = Document()
        document.add_paragraph("第一段会议内容。")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "行动"
        table.cell(0, 1).text = "开展测试"
        document.save(source)
        text = extract_transcript(source)
        self.assertIn("第一段会议内容", text)
        self.assertIn("行动 | 开展测试", text)
        fragments = stable_fragments([
            {"source_id": 5, "position": 0, "text": text, "speaker_labeled": False},
        ], max_chars=12)
        self.assertEqual(fragments[0]["id"], "S001")
        self.assertEqual(
            [item["id"] for item in fragments],
            [f"S{index:03d}" for index in range(1, len(fragments) + 1)],
        )

    def test_word_export_contains_all_sections_tables_and_page_fields(self):
        output = _DATA_DIR / "record.docx"
        record = _structured()
        record["sections"][2]["content"] = "|议题|结论|\n|---|---|\n|消防|开展 POC [S001]|"
        build_docx(
            record,
            output,
            source_fragments=[{
                "id": "S001", "source_id": 9,
                "text": "会议原始片段用于核对。", "speaker_labeled": False,
            }],
        )
        self.assertTrue(output.is_file())
        with zipfile.ZipFile(output) as package:
            document_xml = package.read("word/document.xml").decode("utf-8")
            footer_xml = package.read("word/footer1.xml").decode("utf-8")
        self.assertIn("核心结论", document_xml)
        self.assertIn("转写辨识与复核清单", document_xml)
        self.assertIn("证据片段索引", document_xml)
        self.assertIn("tblHeader", document_xml)
        self.assertIn("PAGE", footer_xml)
        self.assertIn("NUMPAGES", footer_xml)


class ProcessorInputMatrixTests(unittest.TestCase):
    def setUp(self):
        db.init_db()
        con = db.get_conn()
        for table in (
            "sync_events", "record_revisions", "processing_jobs", "meeting_sources",
            "minutes", "attendees", "meetings",
        ):
            con.execute(f"DELETE FROM {table}")
        con.commit()

    def _source(self, meeting_id, name, source_type, text="", pair_key=""):
        path = db.UPLOAD_DIR / name
        path.write_bytes(text.encode("utf-8") if source_type == "transcript" else b"RIFFaudio")
        return db.add_source(
            meeting_id,
            source_type=source_type,
            original_name=name,
            stored_path=str(path),
            sha256=db.file_sha256(path),
            text_content=text,
            pair_key=pair_key,
        )

    def test_authoritative_transcript_is_aligned_while_unpaired_audio_is_transcribed(self):
        meeting_id = db.create_meeting("混合来源", "2026-08-25", "", [])
        transcript = self._source(
            meeting_id, "authoritative.txt", "transcript", "权威识别稿原文。", "pair-A"
        )
        paired_audio = self._source(meeting_id, "paired.wav", "audio", pair_key="pair-A")
        unpaired_audio = self._source(meeting_id, "unpaired.wav", "audio")
        meeting = db.get_meeting(meeting_id)
        job = db.enqueue_job(meeting_id, input_hash(meeting))
        authoritative_inputs = []

        def analyze(_meeting_id, source_id, _path, authoritative_text, _upload_dir):
            authoritative_inputs.append((source_id, authoritative_text))
            if authoritative_text:
                return {
                    "text": authoritative_text,
                    "labeled": "发言人2：权威识别稿原文。",
                    "segments": [{"speaker": 1, "start_ms": 0, "end_ms": 1000}],
                }
            return {
                "text": "自动转写内容。",
                "labeled": "发言人1：自动转写内容。",
                "segments": [{"speaker": 0, "start_ms": 0, "end_ms": 800}],
            }

        with mock.patch("app.processor.spk.available", return_value=True), mock.patch(
            "app.processor.spk.analyze", side_effect=analyze
        ):
            fragments, policy = processor.prepare_fragments(job, meeting)
        self.assertEqual(authoritative_inputs[0], (paired_audio["id"], "权威识别稿原文。"))
        self.assertEqual(authoritative_inputs[1], (unpaired_audio["id"], None))
        self.assertTrue(policy["transcript_authoritative"])
        self.assertTrue(policy["audio_alignment_used"])
        self.assertTrue(policy["audio_asr_used"])
        combined = "\n".join(item["text"] for item in fragments)
        self.assertIn("发言人2：权威识别稿原文", combined)
        self.assertIn("发言人1：自动转写内容", combined)
        generated = [item for item in db.list_sources(meeting_id) if item["generated"]]
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["pair_key"], f"audio:{unpaired_audio['id']}")
        self.assertEqual(transcript["text_content"], "权威识别稿原文。")

    def test_audio_only_fails_visibly_when_funasr_is_unavailable(self):
        meeting_id = db.create_meeting("仅录音", "2026-08-25", "", [])
        self._source(meeting_id, "only.wav", "audio")
        meeting = db.get_meeting(meeting_id)
        job = db.enqueue_job(meeting_id, input_hash(meeting))
        with mock.patch("app.processor.spk.available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "FunASR 环境不可用"):
                processor.prepare_fragments(job, meeting)

    def test_paired_audio_does_not_block_authoritative_transcript_without_funasr(self):
        meeting_id = db.create_meeting("识别稿优先", "2026-08-25", "", [])
        self._source(
            meeting_id, "authority.txt", "transcript", "权威文本保持不变。", "pair-B"
        )
        self._source(meeting_id, "auxiliary.wav", "audio", pair_key="pair-B")
        meeting = db.get_meeting(meeting_id)
        job = db.enqueue_job(meeting_id, input_hash(meeting))
        with mock.patch("app.processor.spk.available", return_value=False):
            fragments, policy = processor.prepare_fragments(job, meeting)
        self.assertEqual(len(fragments), 1)
        self.assertIn("权威文本保持不变", fragments[0]["text"])
        self.assertTrue(policy["transcript_authoritative"])
        self.assertFalse(policy["audio_asr_used"])


class ExactModelTests(unittest.TestCase):
    @staticmethod
    def _response(value):
        return httpx.Response(200, json=value, request=httpx.Request("GET", "http://local"))

    def test_preflight_requires_exact_model_and_idle_scheduler(self):
        health = self._response({
            "status": "healthy",
            "scheduler": {"num_running": 0, "num_waiting": 0},
        })
        models = self._response({"data": [{"id": llm.EXACT_MODEL_ID}]})
        with mock.patch("app.llm.httpx.get", side_effect=[health, models]):
            self.assertEqual(llm.model_preflight()["model_id"], llm.EXACT_MODEL_ID)
        busy = self._response({
            "status": "healthy",
            "scheduler": {"num_running": 1, "num_waiting": 0},
        })
        with mock.patch("app.llm.httpx.get", side_effect=[busy, models]):
            with self.assertRaises(llm.ModelBusyError):
                llm.model_preflight()
        with mock.patch.object(llm, "CONFIGURED_MODEL", "alias-model"):
            with self.assertRaises(llm.ModelIdentityError):
                llm.model_preflight()

    def test_invalid_inline_reference_is_rejected(self):
        value = _structured()
        value["sections"][0]["content"] = "虚构内容 [S999]"
        with self.assertRaisesRegex(ValueError, "虚构片段引用"):
            llm._normalize_final(value, {"title": "测试"}, {"S001"})

    def test_chat_retries_one_timeout_and_sends_only_exact_model(self):
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", "http://local"),
        )
        with mock.patch("app.llm.model_preflight"), mock.patch(
            "app.llm.httpx.post",
            side_effect=[httpx.ReadTimeout("timeout"), response],
        ) as post:
            self.assertEqual(llm._chat([{"role": "user", "content": "x"}], 50), "{}")
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.kwargs["json"]["model"] == llm.EXACT_MODEL_ID for call in post.call_args_list))


class MeetingApiTests(unittest.TestCase):
    def test_upload_response_does_not_echo_transcript_body(self):
        headers = {"X-Meeting-Minutes-Action": "confirm"}
        with TestClient(app) as client:
            created = client.post(
                "/api/meetings",
                headers=headers,
                json={"title": "上传回显测试", "meeting_date": "2026-08-25"},
            )
            meeting_id = created.json()["id"]
            response = client.post(
                f"/api/meetings/{meeting_id}/sources",
                headers=headers,
                data={"text": "不应在响应中回显的识别稿正文"},
            )
            self.assertEqual(response.status_code, 201)
            payload = response.json()
            self.assertNotIn("text", payload)
            self.assertEqual(payload["text_chars"], 14)

    def test_date_is_required_before_organize_and_all_mutations_need_confirmation(self):
        headers = {"X-Meeting-Minutes-Action": "confirm"}
        with TestClient(app) as client:
            missing = client.post("/api/meetings", json={"title": "接口测试"})
            self.assertEqual(missing.status_code, 403)
            created = client.post(
                "/api/meetings", json={"title": "接口测试"}, headers=headers
            )
            self.assertEqual(created.status_code, 201)
            meeting_id = created.json()["id"]
            uploaded = client.post(
                f"/api/meetings/{meeting_id}/sources",
                data={"text": "说话人1：建议先开展测试。", "source_name": "识别稿.txt"},
                headers=headers,
            )
            self.assertEqual(uploaded.status_code, 201)
            organize = client.post(
                f"/api/meetings/{meeting_id}/organize", headers=headers
            )
            self.assertEqual(organize.status_code, 400)
            self.assertIn("会议日期", organize.json()["detail"])
            invalid = client.patch(
                f"/api/meetings/{meeting_id}",
                json={"title": "接口测试", "meeting_date": "2026/08/25"},
                headers=headers,
            )
            self.assertEqual(invalid.status_code, 400)
            updated = client.patch(
                f"/api/meetings/{meeting_id}",
                json={"title": "接口测试", "meeting_date": "2026-08-25"},
                headers=headers,
            )
            self.assertEqual(updated.status_code, 200)
            evil = client.post(
                "/api/meetings",
                json={"title": "跨站"},
                headers={**headers, "Origin": "http://attacker.example"},
            )
            self.assertEqual(evil.status_code, 403)
            cross_scheme = client.post(
                "/api/meetings",
                json={"title": "跨协议"},
                headers={**headers, "Origin": "https://testserver"},
            )
            self.assertEqual(cross_scheme.status_code, 403)


def tearDownModule():
    connection = getattr(db._local, "conn", None)
    if connection is not None:
        connection.close()
        db._local.conn = None
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
