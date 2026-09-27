#!/usr/bin/env bash
# 本地前台启动 HTTPS 会议应用；部署时由 launchd 分别运行应用和 HTTP 跳转。
cd "$(dirname "$0")"
PY=python3
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
exec "$PY" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8766}" \
  --ssl-certfile "${MEETING_TLS_CERT_FILE:-/Users/apple/Workplace/DigitalLab/var/certs/digitallab-local.crt}" \
  --ssl-keyfile "${MEETING_TLS_KEY_FILE:-/Users/apple/Workplace/DigitalLab/var/certs/digitallab-local.key}"
