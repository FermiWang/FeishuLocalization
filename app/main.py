"""FastAPI 入口：会议纪要整理应用，监听 127.0.0.1:8765。"""
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, llm, spk

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="会议纪要整理")
db.init_db()


class Attendee(BaseModel):
    name: str
    role: str = ""


class MeetingCreate(BaseModel):
    title: str
    meeting_date: str = ""
    background: str = ""
    attendees: list[Attendee] = []


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/meetings", status_code=201)
def create_meeting(payload: MeetingCreate):
    if not payload.title.strip():
        raise HTTPException(400, "会议标题不能为空")
    meeting_id = db.create_meeting(
        payload.title.strip(),
        payload.meeting_date.strip(),
        payload.background.strip(),
        [a.model_dump() for a in payload.attendees],
    )
    return {"id": meeting_id}


@app.get("/api/meetings")
def list_meetings():
    return db.list_meetings()


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int):
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, "会议不存在")
    meeting["has_audio"] = bool(meeting["audio_path"])
    meeting["has_transcript"] = bool(meeting["transcript_path"])
    meeting["transcript"] = db.read_transcript(meeting)
    spk_path = db.UPLOAD_DIR / f"{meeting_id}_transcript_spk.txt"
    meeting["transcript_spk"] = (
        spk_path.read_text(encoding="utf-8", errors="replace")
        if spk_path.is_file() else ""
    )
    return meeting


@app.delete("/api/meetings/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int):
    """永久删除会议：数据库记录（含参会人、纪要）与服务器上的录音/转写文件。"""
    if db.get_meeting(meeting_id) is None:
        raise HTTPException(404, "会议不存在")
    for p in db.delete_meeting(meeting_id):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    # 清理分离中间产物（标注版转写、临时 wav/json 等）
    for f in db.UPLOAD_DIR.glob(f"{meeting_id}_*"):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name) or "file"


@app.post("/api/meetings/{meeting_id}/audio")
async def upload_audio(meeting_id: int, file: UploadFile = File(...)):
    if db.get_meeting(meeting_id) is None:
        raise HTTPException(404, "会议不存在")
    name = _safe_filename(file.filename or "audio.mp3")
    if not name.lower().endswith((".mp3", ".m4a", ".wav", ".aac")):
        raise HTTPException(400, "请上传音频文件（mp3 等）")
    dest = db.UPLOAD_DIR / f"{meeting_id}_audio_{name}"
    dest.write_bytes(await file.read())
    db.update_meeting_file(meeting_id, "audio_path", str(dest))
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/transcript")
async def upload_transcript(
    meeting_id: int,
    file: UploadFile | None = File(default=None),
    text: str = Form(default=""),
):
    if db.get_meeting(meeting_id) is None:
        raise HTTPException(404, "会议不存在")
    content = text
    if file is not None and file.filename:
        raw = await file.read()
        content = raw.decode("utf-8", errors="replace")
    if not content.strip():
        raise HTTPException(400, "讯飞识别文字不能为空")
    dest = db.UPLOAD_DIR / f"{meeting_id}_transcript.txt"
    dest.write_text(content, encoding="utf-8")
    db.update_meeting_file(meeting_id, "transcript_path", str(dest))
    return {"ok": True, "chars": len(content)}


@app.get("/api/meetings/{meeting_id}/audio")
def get_audio(meeting_id: int):
    meeting = db.get_meeting(meeting_id)
    if meeting is None or not meeting["audio_path"]:
        raise HTTPException(404, "暂无录音")
    return FileResponse(meeting["audio_path"])


def _run_organize(meeting_id: int) -> None:
    try:
        meeting = db.get_meeting(meeting_id)
        transcript = db.read_transcript(meeting)

        # 第一步：有录音时先做声纹分离，把转写按发言人区分；失败自动回退原文
        if meeting["audio_path"] and spk.available():
            db.set_stage(meeting_id, "声纹分离与发言人标注中（长会议可能需要数十分钟）…")
            labeled = spk.diarize(meeting_id, meeting["audio_path"],
                                  meeting["transcript_path"], db.UPLOAD_DIR)
            if db.get_meeting(meeting_id) is None:
                return  # 分离期间会议已被删除
            if labeled:
                spk_path = db.UPLOAD_DIR / f"{meeting_id}_transcript_spk.txt"
                spk_path.write_text(labeled, encoding="utf-8")
                transcript = labeled

        # 第二步：调用本地大模型整理
        db.set_stage(meeting_id, "模型整理中…")
        result = llm.organize(meeting, transcript)
        if db.get_meeting(meeting_id) is None:
            return  # 整理期间会议已被删除，直接丢弃结果
        db.set_minutes_status(meeting_id, "done", content=result)
        db.set_stage(meeting_id, "")
    except Exception as exc:  # noqa: BLE001 — 任何失败都写回状态供前端展示
        if db.get_meeting(meeting_id) is None:
            return  # 会议已被删除，无需写回失败状态
        db.set_minutes_status(meeting_id, "failed", error=str(exc))
        db.set_stage(meeting_id, "")


@app.post("/api/meetings/{meeting_id}/organize", status_code=202)
def organize(meeting_id: int):
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, "会议不存在")
    if not meeting["transcript_path"]:
        raise HTTPException(400, "请先上传讯飞识别文字")
    current = db.get_minutes(meeting_id)
    if current and current["status"] == "processing":
        return {"status": "processing"}
    db.set_minutes_status(meeting_id, "processing")
    threading.Thread(target=_run_organize, args=(meeting_id,), daemon=True).start()
    return {"status": "processing"}


@app.get("/api/meetings/{meeting_id}/minutes")
def get_minutes(meeting_id: int):
    minutes = db.get_minutes(meeting_id)
    if minutes is None:
        raise HTTPException(404, "会议不存在")
    return minutes


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8765")),
    )
