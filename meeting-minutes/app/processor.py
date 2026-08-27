"""Bounded recoverable workers shared by ASR and the detailed-record model."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import db, llm, spk
from .docx_export import build_docx
from .sources import effective_sources, input_hash, segment_text, stable_fragments

MAX_PARALLEL_JOBS = max(1, min(8, int(os.environ.get("MEETING_MAX_PARALLEL_JOBS", "6"))))
MODEL_RETRY_SECONDS = max(1, int(os.environ.get("MODEL_RETRY_SECONDS", "30")))
_workers: list[threading.Thread] = []
_worker_lock = threading.Lock()
_asr_slot = threading.Semaphore(1)
_wake = threading.Event()
_stop = threading.Event()


def _job_update(job_id: int, meeting_id: int, stage: str, progress: int,
                checkpoint: dict[str, Any] | None = None,
                *, status: str | None = None) -> None:
    db.update_job(job_id, status=status, stage=stage, progress=progress, checkpoint=checkpoint)
    db.set_stage(meeting_id, stage)


def _find_paired_audio(source: dict[str, Any], sources: list[dict[str, Any]],
                       consumed: set[int]) -> dict[str, Any] | None:
    pair_key = str(source.get("pair_key") or "").strip()
    if not pair_key:
        return None
    return next((candidate for candidate in sources
                 if candidate["source_type"] == "audio"
                 and candidate["id"] not in consumed
                 and str(candidate.get("pair_key") or "").strip() == pair_key), None)


def _generated_exists(audio_id: int, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    key = f"audio:{audio_id}"
    return next((source for source in sources
                 if source["source_type"] == "transcript"
                 and source.get("generated")
                 and source.get("pair_key") == key), None)


def _analyze_audio(*args: Any) -> dict[str, Any] | None:
    """Keep the memory-heavy FunASR lane serial while text jobs run in parallel."""
    with _asr_slot:
        return spk.analyze(*args)


def prepare_fragments(job: dict[str, Any], meeting: dict[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    """Apply transcript-authority and audio-only ASR rules, returning stable fragments."""
    all_sources = list(meeting.get("sources") or [])
    sources = effective_sources(all_sources)
    effective_ids = {source.get("id") for source in sources}
    duplicate_ids = [
        int(source["id"]) for source in all_sources
        if source.get("id") not in effective_ids
    ]
    segments: list[dict[str, Any]] = []
    consumed_audio: set[int] = set()
    used_asr = False
    used_alignment = False

    for source in sources:
        if source["source_type"] != "transcript" or source.get("generated"):
            continue
        text = db.source_text(source)
        if not text.strip():
            continue
        paired = _find_paired_audio(source, sources, consumed_audio)
        if paired:
            # A paired recording is always auxiliary.  Even when FunASR is
            # unavailable or alignment fails, do not reinterpret it as an
            # unpaired audio-only source and overwrite the transcript policy.
            consumed_audio.add(paired["id"])
        if paired and spk.available():
            _job_update(job["id"], meeting["id"],
                        f"识别稿优先：为 {source['original_name']} 对齐时间轴和说话人", 5)
            db.set_source_processing(paired["id"], "processing")
            analysis = _analyze_audio(
                meeting["id"], paired["id"], paired["stored_path"], text, db.UPLOAD_DIR
            )
            if analysis:
                used_alignment = True
                db.set_source_processing(paired["id"], "ready")
                labeled = str(analysis.get("labeled") or "").strip()
                segments.append(segment_text(
                    source["id"], source["position"], labeled or text,
                    speaker_labeled=bool(labeled), timeline=analysis.get("segments") or [],
                ))
                continue
            db.set_source_processing(paired["id"], "failed", "说话人/时间轴对齐失败，已使用权威识别稿")
        segments.append(segment_text(
            source["id"], source["position"], text,
            speaker_labeled=bool("发言人" in text),
        ))

    for audio in sources:
        if audio["source_type"] != "audio" or audio["id"] in consumed_audio:
            continue
        existing = _generated_exists(audio["id"], sources)
        if existing:
            text = db.source_text(existing)
            segments.append(segment_text(
                existing["id"], audio["position"], text,
                speaker_labeled=bool("发言人" in text),
            ))
            consumed_audio.add(audio["id"])
            used_asr = True
            continue
        if not spk.available():
            raise RuntimeError("仅提供录音，但 FunASR 环境不可用，无法生成识别稿")
        _job_update(job["id"], meeting["id"],
                    f"FunASR 转写与说话人分离：{audio['original_name']}", 5)
        db.set_source_processing(audio["id"], "processing")
        analysis = _analyze_audio(
            meeting["id"], audio["id"], audio["stored_path"], None, db.UPLOAD_DIR
        )
        if not analysis:
            db.set_source_processing(audio["id"], "failed", "FunASR 未返回有效识别稿")
            raise RuntimeError(f"录音 {audio['original_name']} 识别失败")
        labeled = str(analysis.get("labeled") or analysis.get("text") or "").strip()
        transcript_path = db.UPLOAD_DIR / f"{meeting['id']}_audio_{audio['id']}_funasr.txt"
        transcript_path.write_text(labeled, encoding="utf-8")
        pair_key = f"audio:{audio['id']}"
        db.set_source_pair(meeting["id"], audio["id"], pair_key)
        generated = db.add_source(
            meeting["id"], source_type="transcript",
            original_name=f"{Path(audio['original_name']).stem}_FunASR.txt",
            stored_path=str(transcript_path), sha256=db.file_sha256(transcript_path),
            text_content=labeled, pair_key=pair_key, generated=True,
        )
        db.set_source_processing(audio["id"], "ready")
        segments.append(segment_text(
            generated["id"], audio["position"], labeled,
            speaker_labeled=True, timeline=analysis.get("segments") or [],
        ))
        consumed_audio.add(audio["id"])
        used_asr = True

    if not segments:
        raise ValueError("没有可用识别稿或可转写录音")
    policy = {
        "transcript_authoritative": any(
            source["source_type"] == "transcript" and not source.get("generated") for source in sources
        ),
        "audio_asr_used": used_asr,
        "audio_alignment_used": used_alignment,
        "speaker_identity_policy": "不可靠时保留说话人N或不署名，不猜测真实姓名",
        "duplicate_sources_skipped": duplicate_ids,
    }
    return stable_fragments(segments), policy


def process_job(job: dict[str, Any]) -> None:
    meeting_id = int(job["meeting_id"])
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        return
    checkpoint = json.loads(job.get("checkpoint_json") or "{}")
    try:
        fragments, policy = prepare_fragments(job, meeting)
        current_input_hash = input_hash(meeting)
        if current_input_hash != str(job.get("input_hash") or ""):
            checkpoint = llm.reconcile_checkpoint(checkpoint, fragments)
            db.reset_job_input(
                job["id"], current_input_hash, checkpoint=checkpoint,
            )
            job = db.get_job(job["id"]) or job
        meeting = db.get_meeting(meeting_id) or meeting
        meeting["source_policy"] = policy

        def progress(stage: str, value: int,
                     new_checkpoint: dict[str, Any] | None) -> None:
            _job_update(job["id"], meeting_id, stage, value, new_checkpoint)

        record = llm.organize(meeting, fragments, checkpoint=checkpoint, on_progress=progress)
        latest_meeting = db.get_meeting(meeting_id)
        if latest_meeting is None:
            return
        latest_input_hash = input_hash(latest_meeting)
        if latest_input_hash != str(job.get("input_hash") or ""):
            db.reset_job_input(job["id"], latest_input_hash, status="queued")
            db.set_stage(meeting_id, "输入在整理期间发生变化，已重新排队")
            _wake.set()
            return
        markdown = llm.structured_to_markdown(record)
        current = db.get_record(meeting_id)
        saved = db.save_revision(
            meeting_id, record, markdown, input_hash=job["input_hash"],
            model_id=llm.EXACT_MODEL_ID, prompt_version=llm.PROMPT_VERSION,
            editor_kind="model", base_revision=current["revision"] if current else 0,
            source_fragments=fragments,
        )
        _job_update(job["id"], meeting_id, "生成 Word 文档", 94)
        docx_path = db.EXPORT_DIR / f"meeting_{meeting_id}_r{saved['revision']}.docx"
        build_docx(
            saved["structured"], docx_path,
            source_fragments=saved.get("source_fragments") or fragments,
        )
        db.set_revision_docx(meeting_id, saved["revision"], str(docx_path))
        db.set_minutes_status(meeting_id, "done", content=markdown)
        db.set_stage(meeting_id, "")
        db.finish_job(job["id"])
    except (llm.ModelBusyError, llm.ModelTemporaryError) as exc:
        db.update_job(
            job["id"], status="waiting", stage=str(exc), progress=1,
            last_error_code="temporary_model_error",
        )
        db.record_job_event(
            job["id"], status="waiting", stage=str(exc),
            error_code="temporary_model_error", message=str(exc),
        )
        db.set_stage(meeting_id, str(exc))
        _stop.wait(MODEL_RETRY_SECONDS)
        if db.get_job(job["id"]):
            db.update_job(job["id"], status="queued", stage="从已保存断点重新排队", progress=1)
            _wake.set()
    except llm.ModelDeterministicError as exc:
        count = db.record_deterministic_failure(
            job["id"], fingerprint=exc.fingerprint,
            error_code=exc.code, message=str(exc),
        )
        if count >= 3:
            message = f"{exc}；相同请求连续失败 {count} 次，已停止自动重试"
            db.finish_job(job["id"], error=message, error_code=exc.code)
            db.set_minutes_status(meeting_id, "failed", error=message)
            db.set_stage(meeting_id, "")
        else:
            stage = f"{exc}；第 {count}/3 次，保留断点后重试"
            db.update_job(job["id"], status="waiting", stage=stage, progress=1)
            db.set_stage(meeting_id, stage)
            _stop.wait(MODEL_RETRY_SECONDS)
            if db.get_job(job["id"]):
                db.update_job(
                    job["id"], status="queued",
                    stage="确定性输出失败后从断点重新排队", progress=1,
                )
                _wake.set()
    except Exception as exc:  # noqa: BLE001 - persisted for visible recovery
        if db.get_meeting(meeting_id) is not None:
            db.finish_job(job["id"], error=str(exc), error_code="unexpected_error")
            db.set_minutes_status(meeting_id, "failed", error=str(exc))
            db.set_stage(meeting_id, "")


def process_next() -> bool:
    job = db.claim_next_job()
    if job is None:
        return False
    process_job(job)
    return True


def _loop() -> None:
    while not _stop.is_set():
        if not process_next():
            _wake.wait(5)
            _wake.clear()


def start_worker() -> None:
    global _workers
    with _worker_lock:
        _workers = [worker for worker in _workers if worker.is_alive()]
        if _workers:
            return
        db.recover_jobs()
        _stop.clear()
        _workers = [
            threading.Thread(
                target=_loop, name=f"meeting-processing-{index + 1}", daemon=True
            )
            for index in range(MAX_PARALLEL_JOBS)
        ]
        for worker in _workers:
            worker.start()


def wake_worker() -> None:
    _wake.set()


def stop_worker() -> None:
    global _workers
    _stop.set()
    _wake.set()
    with _worker_lock:
        workers = list(_workers)
        _workers = []
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout=1)


def worker_status() -> dict[str, int]:
    return {
        "configured_parallel_jobs": MAX_PARALLEL_JOBS,
        "active_workers": sum(worker.is_alive() for worker in _workers),
    }
