"""Audio transcription and speaker diarization orchestration.

主应用自身不依赖 torch/funasr；worker 由独立 venv（SPK_PYTHON）执行。
任何一步失败都返回 None，由调用方回退为无发言人标注的整理。
"""
import json
import os
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# funasr 环境的 python（远端为 ~/meeting-minutes/.venv-spk）
SPK_PYTHON = os.environ.get(
    "SPK_PYTHON", str(BASE_DIR.parent / ".venv-spk" / "bin" / "python3")
)
WORKER = BASE_DIR / "diarize_worker.py"
# 1 小时会议约需 20–30 分钟，留足余量
WORKER_TIMEOUT = int(os.environ.get("SPK_TIMEOUT", "7200"))


def available() -> bool:
    return Path(SPK_PYTHON).is_file() and WORKER.is_file()


def analyze(meeting_id: int, source_id: int, audio_path: str,
            authoritative_text: str | None, upload_dir: Path) -> dict | None:
    """Return FunASR text/timeline/speaker labels; caller decides fallback policy."""
    if not available():
        return None
    stem = f"{meeting_id}_{source_id}_spk"
    wav = upload_dir / f"{stem}.wav"
    transcript_path = upload_dir / f"{stem}_authoritative.txt"
    out_json = upload_dir / f"{stem}.json"
    try:
        # macOS 自带 afconvert：mp3/m4a 等 → 16kHz 单声道 wav
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             audio_path, str(wav)],
            check=True, capture_output=True, timeout=300,
        )
        transcript_arg = "-"
        if authoritative_text and authoritative_text.strip():
            transcript_path.write_text(authoritative_text, encoding="utf-8")
            transcript_arg = str(transcript_path)
        subprocess.run(
            [SPK_PYTHON, str(WORKER), str(wav), transcript_arg, str(out_json)],
            check=True, capture_output=True, timeout=WORKER_TIMEOUT,
        )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        if not str(data.get("text") or "").strip():
            return None
        return data
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, json.JSONDecodeError, KeyError):
        return None
    finally:
        wav.unlink(missing_ok=True)


def diarize(meeting_id: int, audio_path: str, transcript_path: str,
            upload_dir: Path) -> str | None:
    """Legacy compatibility wrapper for the original single-source API."""
    text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    result = analyze(meeting_id, 0, audio_path, text, upload_dir)
    return str(result.get("labeled") or "").strip() if result else None
