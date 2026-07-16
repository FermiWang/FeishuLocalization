#!/bin/sh
set -eu

LABEL="com.fermiwang.feishu-archive"
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCHIVE_DIR=${FEISHU_ARCHIVE_DIR:-"$HOME/Library/Application Support/Feishu Archive"}
RUNTIME_DIR="$ARCHIVE_DIR/runtime"
LOG_DIR="$ARCHIVE_DIR/logs"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_BIN=$(command -v python3)
USER_DOMAIN="gui/$(id -u)"

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Feishu Archive 需要 Python 3.11 或更高版本。" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR/bin" "$RUNTIME_DIR/src" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$ARCHIVE_DIR" "$RUNTIME_DIR" "$LOG_DIR"

rsync -a --delete "$PROJECT_ROOT/src/" "$RUNTIME_DIR/src/"
install -m 755 "$PROJECT_ROOT/bin/feishu-archive" "$RUNTIME_DIR/bin/feishu-archive"
install -m 644 "$PROJECT_ROOT/pyproject.toml" "$RUNTIME_DIR/pyproject.toml"

"$RUNTIME_DIR/bin/feishu-archive" --archive-dir "$ARCHIVE_DIR" init
chmod 600 "$ARCHIVE_DIR/archive.sqlite3"

TEMP_PLIST=$(mktemp)
rm -f "$TEMP_PLIST"
trap 'rm -f "$TEMP_PLIST"' EXIT

/usr/libexec/PlistBuddy -c "Add :Label string $LABEL" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string serve" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:4 string --host" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:5 string 127.0.0.1" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:6 string --port" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:7 string 8765" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :KeepAlive dict" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :KeepAlive:SuccessfulExit bool false" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Interactive" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 5" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/service.log" "$TEMP_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/service.error.log" "$TEMP_PLIST"

plutil -lint "$TEMP_PLIST" >/dev/null
install -m 600 "$TEMP_PLIST" "$PLIST_PATH"

launchctl bootout "$USER_DOMAIN/$LABEL" >/dev/null 2>&1 || true
sleep 0.5
launchctl enable "$USER_DOMAIN/$LABEL"
launchctl bootstrap "$USER_DOMAIN" "$PLIST_PATH"

ATTEMPT=0
while [ "$ATTEMPT" -lt 20 ]; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8765/api/status >/dev/null; then
    echo "Feishu Archive 已部署：http://127.0.0.1:8765"
    echo "档案目录：$ARCHIVE_DIR"
    echo "服务标识：$LABEL"
    exit 0
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 0.5
done

echo "服务未在预期时间内就绪，请检查：$LOG_DIR/service.error.log" >&2
exit 1
