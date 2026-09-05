"""FastAPI entry point for multi-source detailed meeting-record generation."""
from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, llm, processor
from .docx_export import build_docx
from .sources import (
    annotate_duplicate_sources,
    extract_transcript,
    input_hash,
    safe_filename,
    source_type_for_filename,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024**3)))
MAX_TRANSCRIPT_BYTES = int(os.environ.get("MAX_TRANSCRIPT_BYTES", str(50 * 1024**2)))
CONFIRM_HEADER = "confirm"

app = FastAPI(title="详细会议记录整理")
db.init_db()


class Attendee(BaseModel):
    name: str
    role: str = ""


class MeetingCreate(BaseModel):
    title: str
    meeting_date: str = ""
    background: str = Field(default="", max_length=50_000)
    attendees: list[Attendee] = Field(default_factory=list)


class MeetingUpdate(BaseModel):
    title: str
    meeting_date: str
    background: str = Field(default="", max_length=50_000)


class SourceOrder(BaseModel):
    source_ids: list[int]


class SourcePair(BaseModel):
    pair_key: str


class RecordEdit(BaseModel):
    base_revision: int
    sections: dict[str, str] | None = None
    structured: dict | None = None


def _model_dump(value: BaseModel) -> dict:
    return value.model_dump() if hasattr(value, "model_dump") else value.dict()


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin:
            parsed_origin = urlsplit(origin)
            origin_host = parsed_origin.netloc.lower()
            request_host = request.headers.get("host", "").lower()
            if origin_host != request_host or parsed_origin.scheme != request.url.scheme:
                return JSONResponse({"detail": "拒绝跨站变更请求"}, status_code=403)
        if request.headers.get("X-Meeting-Minutes-Action") != CONFIRM_HEADER:
            return JSONResponse(
                {"detail": "变更请求缺少确认头 X-Meeting-Minutes-Action: confirm"},
                status_code=403,
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _require_confirmation(value: str | None) -> None:
    if value != CONFIRM_HEADER:
        raise HTTPException(403, "变更请求缺少确认头 X-Meeting-Minutes-Action: confirm")


def _meeting_or_404(meeting_id: int) -> dict:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, "会议不存在")
    return meeting


def _validate_meeting_date(value: str, *, required: bool) -> str:
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise HTTPException(400, "会议日期为必填项")
        return ""
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(400, "会议日期必须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != cleaned:
        raise HTTPException(400, "会议日期必须为 YYYY-MM-DD")
    return cleaned


def _public_source(source: dict, *, include_text: bool = False) -> dict:
    result = {
        key: source.get(key)
        for key in (
            "id", "meeting_id", "source_type", "position", "pair_key", "original_name",
            "sha256", "generated", "processing_status", "error", "created_at", "updated_at",
            "duplicate_of_source_id",
        )
    }
    if include_text and source.get("source_type") == "transcript":
        result["text"] = db.source_text(source)
    return result


def _public_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {
        key: job.get(key)
        for key in (
            "id", "meeting_id", "status", "stage", "progress", "error",
            "attempts", "heartbeat_at", "last_error_code", "same_failure_count",
            "created_at", "started_at", "updated_at", "finished_at",
        )
    }


def _public_record(record: dict) -> dict:
    result = dict(record)
    result.pop("docx_path", None)
    fragments = result.pop("source_fragments", [])
    result["source_fragment_count"] = len(fragments)
    return result


async def _save_upload(file: UploadFile, destination: Path, limit: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as handle:
        while True:
            block = await file.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > limit:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(413, f"上传文件超过限制（{limit // 1024**2} MB）")
            digest.update(block)
            handle.write(block)
    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "上传文件为空")
    return size, digest.hexdigest()


@app.on_event("startup")
def startup() -> None:
    processor.start_worker()


@app.on_event("shutdown")
def shutdown() -> None:
    processor.stop_worker()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/system/model")
def model_status():
    workers = processor.worker_status()
    try:
        result = llm.model_preflight()
        return {
            "status": "ready", "model_id": result["model_id"],
            "scheduler": result["scheduler"],
            "model_max_concurrency": result["max_concurrency"],
            "model_max_waiting_requests": result["max_waiting_requests"],
            "model_request_priority": result["request_priority"], **workers,
        }
    except llm.ModelBusyError as exc:
        return {
            "status": "busy", "model_id": llm.EXACT_MODEL_ID,
            "model_max_concurrency": llm.MODEL_MAX_CONCURRENCY,
            "model_max_waiting_requests": llm.MODEL_MAX_WAITING_REQUESTS,
            "model_request_priority": llm.MEETING_MODEL_PRIORITY,
            "detail": str(exc), **workers,
        }
    except Exception as exc:  # noqa: BLE001 - health endpoint
        return {
            "status": "unavailable", "model_id": llm.EXACT_MODEL_ID,
            "model_max_concurrency": llm.MODEL_MAX_CONCURRENCY,
            "model_max_waiting_requests": llm.MODEL_MAX_WAITING_REQUESTS,
            "model_request_priority": llm.MEETING_MODEL_PRIORITY,
            "detail": str(exc), **workers,
        }


@app.post("/api/meetings", status_code=201)
def create_meeting(payload: MeetingCreate):
    if not payload.title.strip():
        raise HTTPException(400, "会议标题不能为空")
    meeting_id = db.create_meeting(
        payload.title.strip(), _validate_meeting_date(payload.meeting_date, required=False),
        payload.background.strip(),
        [_model_dump(attendee) for attendee in payload.attendees],
    )
    return {"id": meeting_id}


@app.patch("/api/meetings/{meeting_id}")
def update_meeting(meeting_id: int, payload: MeetingUpdate,
                   x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    meeting = _meeting_or_404(meeting_id)
    if not payload.title.strip():
        raise HTTPException(400, "会议标题不能为空")
    meeting_date = _validate_meeting_date(payload.meeting_date, required=True)
    if (payload.title.strip(), meeting_date, payload.background.strip()) == (
        meeting["title"], meeting["meeting_date"], meeting["background"],
    ):
        return {"ok": True, "revision": meeting.get("current_revision") or 0}
    db.update_meeting(
        meeting_id, title=payload.title.strip(),
        meeting_date=meeting_date, background=payload.background.strip(),
    )
    current = db.get_record(meeting_id)
    if current is None:
        return {"ok": True}
    structured = dict(current["structured"])
    structured["title"] = payload.title.strip()
    structured["meeting_meta"] = {
        **(structured.get("meeting_meta") or {}),
        "title": payload.title.strip(), "meeting_date": meeting_date,
        "background": payload.background.strip(), "attendees": meeting["attendees"],
    }
    markdown = llm.structured_to_markdown(structured)
    saved = db.save_revision(
        meeting_id, structured, markdown,
        input_hash=input_hash(db.get_meeting(meeting_id) or meeting),
        model_id=current["model_id"], prompt_version=current["prompt_version"],
        editor_kind="user", base_revision=current["revision"],
    )
    docx_path = db.EXPORT_DIR / f"meeting_{meeting_id}_r{saved['revision']}.docx"
    build_docx(
        saved["structured"], docx_path,
        source_fragments=saved.get("source_fragments") or [],
    )
    db.set_revision_docx(meeting_id, saved["revision"], str(docx_path))
    return {"ok": True, "revision": saved["revision"]}


@app.get("/api/meetings")
def list_meetings():
    return db.list_meetings()


@app.post("/api/meetings/{meeting_id}/background/refresh")
def refresh_background(meeting_id: int,
                       x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    meeting = _meeting_or_404(meeting_id)
    try:
        refreshed = processor.prepare_background(meeting, force=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"items": refreshed.get("background_pages") or []}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int):
    meeting = _meeting_or_404(meeting_id)
    meeting.pop("audio_path", None)
    meeting.pop("transcript_path", None)
    meeting["sources"] = [
        _public_source(source)
        for source in annotate_duplicate_sources(meeting["sources"])
    ]
    meeting["job"] = _public_job(meeting.get("job"))
    meeting["has_audio"] = any(source["source_type"] == "audio" for source in meeting["sources"])
    meeting["has_transcript"] = any(source["source_type"] == "transcript" for source in meeting["sources"])
    return meeting


@app.delete("/api/meetings/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int,
                   x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    _meeting_or_404(meeting_id)
    for path in db.delete_meeting(meeting_id):
        try:
            resolved = Path(path).resolve()
            if db.DATA_DIR.resolve() in resolved.parents:
                resolved.unlink(missing_ok=True)
        except OSError:
            pass


@app.get("/api/meetings/{meeting_id}/sources")
def list_sources(meeting_id: int, include_text: bool = False):
    _meeting_or_404(meeting_id)
    return [
        _public_source(source, include_text=include_text)
        for source in annotate_duplicate_sources(db.list_sources(meeting_id))
    ]


@app.post("/api/meetings/{meeting_id}/sources", status_code=201)
async def upload_source(meeting_id: int, file: UploadFile | None = File(default=None),
                        text: str = Form(default=""), pair_key: str = Form(default=""),
                        source_name: str = Form(default="粘贴识别稿.txt")):
    _meeting_or_404(meeting_id)
    if file is None and not text.strip():
        raise HTTPException(400, "请选择文件或粘贴识别稿")
    if file is not None and file.filename:
        try:
            source_type = source_type_for_filename(file.filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        name = safe_filename(file.filename)
        destination = db.UPLOAD_DIR / f"{meeting_id}_{name}"
        suffix = 1
        while destination.exists():
            destination = db.UPLOAD_DIR / f"{meeting_id}_{suffix}_{name}"
            suffix += 1
        limit = MAX_AUDIO_BYTES if source_type == "audio" else MAX_TRANSCRIPT_BYTES
        _, digest = await _save_upload(file, destination, limit)
        duplicate = db.find_duplicate_source(
            meeting_id, source_type=source_type, sha256=digest, pair_key=pair_key,
        )
        if duplicate is not None:
            destination.unlink(missing_ok=True)
            result = _public_source(duplicate)
            result["duplicate_skipped"] = True
            result["text_chars"] = len(db.source_text(duplicate)) if source_type == "transcript" else 0
            return result
        extracted = ""
        if source_type == "transcript":
            try:
                extracted = extract_transcript(destination)
            except Exception as exc:
                destination.unlink(missing_ok=True)
                raise HTTPException(400, str(exc)) from exc
        source = db.add_source(
            meeting_id, source_type=source_type, original_name=file.filename,
            stored_path=str(destination), sha256=digest, text_content=extracted,
            pair_key=pair_key,
        )
    else:
        content = text.strip()
        raw = content.encode("utf-8")
        if len(raw) > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(413, "粘贴识别稿超过大小限制")
        digest = hashlib.sha256(raw).hexdigest()
        duplicate = db.find_duplicate_source(
            meeting_id, source_type="transcript", sha256=digest, pair_key=pair_key,
        )
        if duplicate is not None:
            result = _public_source(duplicate)
            result["duplicate_skipped"] = True
            result["text_chars"] = len(content)
            return result
        name = safe_filename(source_name or "粘贴识别稿.txt")
        destination = db.UPLOAD_DIR / f"{meeting_id}_{name}"
        suffix = 1
        while destination.exists():
            destination = db.UPLOAD_DIR / f"{meeting_id}_{suffix}_{name}"
            suffix += 1
        destination.write_text(content, encoding="utf-8")
        source = db.add_source(
            meeting_id, source_type="transcript", original_name=source_name,
            stored_path=str(destination), sha256=digest,
            text_content=content, pair_key=pair_key,
        )
    result = _public_source(source)
    result["text_chars"] = len(extracted if file is not None else content)
    return result


@app.delete("/api/meetings/{meeting_id}/sources/{source_id}", status_code=204)
def delete_source(meeting_id: int, source_id: int,
                  x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    _meeting_or_404(meeting_id)
    try:
        path = db.delete_source(meeting_id, source_id)
    except KeyError as exc:
        raise HTTPException(404, "来源不存在") from exc
    try:
        resolved = Path(path).resolve()
        if db.UPLOAD_DIR.resolve() in resolved.parents:
            resolved.unlink(missing_ok=True)
    except OSError:
        pass


@app.put("/api/meetings/{meeting_id}/sources/order")
def reorder_sources(meeting_id: int, payload: SourceOrder,
                    x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    _meeting_or_404(meeting_id)
    try:
        db.reorder_sources(meeting_id, payload.source_ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.put("/api/meetings/{meeting_id}/sources/{source_id}/pair")
def pair_source(meeting_id: int, source_id: int, payload: SourcePair,
                x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    _meeting_or_404(meeting_id)
    try:
        db.set_source_pair(meeting_id, source_id, payload.pair_key)
    except KeyError as exc:
        raise HTTPException(404, "来源不存在") from exc
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}/sources/{source_id}/audio")
def get_source_audio(meeting_id: int, source_id: int):
    source = db.get_source(meeting_id, source_id)
    if source is None or source["source_type"] != "audio":
        raise HTTPException(404, "录音来源不存在")
    path = Path(source["stored_path"]).resolve()
    if db.UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "录音文件不存在")
    filename = safe_filename(source["original_name"])
    return FileResponse(
        path, media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
        },
    )


# Original single-audio and single-transcript routes remain compatible.
@app.post("/api/meetings/{meeting_id}/audio")
async def upload_audio(meeting_id: int, file: UploadFile = File(...)):
    return await upload_source(meeting_id, file=file, text="", pair_key="legacy-1")


@app.post("/api/meetings/{meeting_id}/transcript")
async def upload_transcript(meeting_id: int, file: UploadFile | None = File(default=None),
                            text: str = Form(default="")):
    source = await upload_source(
        meeting_id, file=file, text=text, pair_key="legacy-1", source_name="粘贴识别稿.txt",
    )
    return {"ok": True, "chars": int(source.get("text_chars") or 0), "source_id": source["id"]}


@app.get("/api/meetings/{meeting_id}/audio")
def get_audio(meeting_id: int):
    source = next((item for item in db.list_sources(meeting_id) if item["source_type"] == "audio"), None)
    if source is None:
        raise HTTPException(404, "暂无录音")
    return get_source_audio(meeting_id, source["id"])


@app.post("/api/meetings/{meeting_id}/organize", status_code=202)
def organize(meeting_id: int,
             x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    meeting = _meeting_or_404(meeting_id)
    _validate_meeting_date(meeting["meeting_date"], required=True)
    if not meeting["sources"]:
        raise HTTPException(400, "请至少上传一份录音或识别稿")
    job = db.enqueue_job(meeting_id, input_hash(meeting))
    processor.wake_worker()
    return {"status": job["status"], "job_id": job["id"], "stage": job["stage"]}


@app.get("/api/meetings/{meeting_id}/jobs/latest")
def latest_job(meeting_id: int):
    _meeting_or_404(meeting_id)
    return _public_job(db.get_latest_job(meeting_id)) or {"status": "pending", "stage": ""}


@app.get("/api/meetings/{meeting_id}/records")
def current_record(meeting_id: int):
    _meeting_or_404(meeting_id)
    record = db.get_record(meeting_id)
    if record is None:
        raise HTTPException(404, "尚未生成详细会议记录")
    return _public_record(record)


@app.get("/api/meetings/{meeting_id}/records/revisions")
def revision_history(meeting_id: int):
    _meeting_or_404(meeting_id)
    return db.list_revisions(meeting_id)


@app.get("/api/meetings/{meeting_id}/records/{revision}")
def get_record(meeting_id: int, revision: int):
    _meeting_or_404(meeting_id)
    record = db.get_record(meeting_id, revision)
    if record is None:
        raise HTTPException(404, "修订版不存在")
    return _public_record(record)


@app.patch("/api/meetings/{meeting_id}/records")
def edit_record(meeting_id: int, payload: RecordEdit,
                x_meeting_minutes_action: str | None = Header(default=None)):
    _require_confirmation(x_meeting_minutes_action)
    meeting = _meeting_or_404(meeting_id)
    current = db.get_record(meeting_id)
    if current is None:
        raise HTTPException(404, "尚未生成详细会议记录")
    structured = dict(current["structured"])
    if payload.structured is not None:
        candidate_sections = payload.structured.get("sections")
        if not isinstance(candidate_sections, list):
            raise HTTPException(400, "结构化修订必须包含章节列表")
        payload.sections = {
            str(section.get("id")): str(section.get("content") or "")
            for section in candidate_sections if isinstance(section, dict) and section.get("id")
        }
    if payload.sections is not None:
        replacements = payload.sections
        valid_ids = {str(section.get("id")) for section in structured.get("sections") or []}
        if set(replacements) - valid_ids:
            raise HTTPException(400, "修订包含未知章节")
        if sum(len(str(value)) for value in replacements.values()) > 2_000_000:
            raise HTTPException(413, "修订内容超过大小限制")
        structured["sections"] = [
            {**section, "content": replacements.get(section["id"], section.get("content", ""))}
            for section in structured.get("sections") or []
        ]
        core = next(
            (section for section in structured["sections"] if section.get("id") == "core-conclusion"),
            None,
        )
        if core is not None:
            structured["core_conclusion"] = core.get("content") or ""
    structured["meeting_meta"] = {
        **(structured.get("meeting_meta") or {}),
        "title": meeting["title"], "meeting_date": meeting["meeting_date"],
        "background": meeting["background"], "attendees": meeting["attendees"],
    }
    markdown = llm.structured_to_markdown(structured)
    try:
        saved = db.save_revision(
            meeting_id, structured, markdown, input_hash=current["input_hash"],
            model_id=current["model_id"], prompt_version=current["prompt_version"],
            editor_kind="user", base_revision=payload.base_revision,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    docx_path = db.EXPORT_DIR / f"meeting_{meeting_id}_r{saved['revision']}.docx"
    build_docx(
        saved["structured"], docx_path,
        source_fragments=saved.get("source_fragments") or [],
    )
    db.set_revision_docx(meeting_id, saved["revision"], str(docx_path))
    db.set_minutes_status(meeting_id, "done", content=markdown)
    return _public_record(db.get_record(meeting_id, saved["revision"]) or saved)


@app.get("/api/meetings/{meeting_id}/records/{revision}/docx")
def download_docx(meeting_id: int, revision: int):
    meeting = _meeting_or_404(meeting_id)
    record = db.get_record(meeting_id, revision)
    if record is None:
        raise HTTPException(404, "修订版不存在")
    path = Path(record["docx_path"] or "").resolve()
    if db.EXPORT_DIR.resolve() not in path.parents or not path.is_file():
        path = db.EXPORT_DIR / f"meeting_{meeting_id}_r{revision}.docx"
        build_docx(
            record["structured"], path,
            source_fragments=record.get("source_fragments") or [],
        )
        db.set_revision_docx(meeting_id, revision, str(path))
    filename = safe_filename(f"{meeting['title']}_详细会议记录_r{revision}.docx")
    return FileResponse(
        path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@app.get("/api/meetings/{meeting_id}/records/{revision}/fragments")
def record_fragments(meeting_id: int, revision: int):
    _meeting_or_404(meeting_id)
    record = db.get_record(meeting_id, revision)
    if record is None:
        raise HTTPException(404, "修订版不存在")
    return {"items": record.get("source_fragments") or []}


@app.get("/api/meetings/{meeting_id}/minutes")
def get_minutes(meeting_id: int):
    minutes = db.get_minutes(meeting_id)
    if minutes is None:
        raise HTTPException(404, "会议不存在")
    record = db.get_record(meeting_id)
    if record:
        minutes["content"] = record["markdown"]
        minutes["revision"] = record["revision"]
    return minutes


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8765")))
