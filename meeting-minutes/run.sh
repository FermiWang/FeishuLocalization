#!/usr/bin/env bash
# 启动会议纪要整理应用，默认监听 0.0.0.0:8765（局域网可访问）
cd "$(dirname "$0")"
PY=python3
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
exec "$PY" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8765}"
