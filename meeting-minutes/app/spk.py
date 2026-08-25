"""说话人分离编排：音频转码 + 调用独立 funasr worker 子进程。

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


def diarize(meeting_id: int, audio_path: str, transcript_path: str,
            upload_dir: Path) -> str | None:
    """对会议音频做声纹分离并把讯飞转写按发言人区分。

    成功返回带「发言人N：」前缀的转写文本；单人会议或失败返回 None。
    """
    if not available():
        return None
    wav = upload_dir / f"{meeting_id}_spk.wav"
    out_json = upload_dir / f"{meeting_id}_spk.json"
    try:
        # macOS 自带 afconvert：mp3/m4a 等 → 16kHz 单声道 wav
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             audio_path, str(wav)],
            check=True, capture_output=True, timeout=300,
        )
        subprocess.run(
            [SPK_PYTHON, str(WORKER), str(wav), transcript_path, str(out_json)],
            check=True, capture_output=True, timeout=WORKER_TIMEOUT,
        )
        data = json.loads(out_json.read_text(encoding="utf-8"))
        labeled = (data.get("labeled") or "").strip()
        return labeled or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, json.JSONDecodeError, KeyError):
        return None
    finally:
        wav.unlink(missing_ok=True)
