#!/bin/sh
set -eu

SERVICE_LABEL="com.fermiwang.feishu-archive"
SYNC_LABEL="com.fermiwang.feishu-archive-sync"
WIKI_SYNC_LABEL="com.fermiwang.feishu-archive-wiki-sync"
MAIL_SYNC_LABEL="com.fermiwang.feishu-archive-mail-sync"
INSIGHTS_LABEL="com.fermiwang.feishu-archive-insights"
INSIGHTS_BACKFILL_LABEL="com.fermiwang.feishu-archive-insights-backfill"
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCHIVE_DIR=${FEISHU_ARCHIVE_DIR:-"$HOME/Library/Application Support/Feishu Archive"}
RUNTIME_DIR="$ARCHIVE_DIR/runtime"
LOG_DIR="$ARCHIVE_DIR/logs"
SERVICE_PLIST_PATH="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
SYNC_PLIST_PATH="$HOME/Library/LaunchAgents/$SYNC_LABEL.plist"
WIKI_SYNC_PLIST_PATH="$HOME/Library/LaunchAgents/$WIKI_SYNC_LABEL.plist"
MAIL_SYNC_PLIST_PATH="$HOME/Library/LaunchAgents/$MAIL_SYNC_LABEL.plist"
INSIGHTS_PLIST_PATH="$HOME/Library/LaunchAgents/$INSIGHTS_LABEL.plist"
INSIGHTS_BACKFILL_PLIST_PATH="$HOME/Library/LaunchAgents/$INSIGHTS_BACKFILL_LABEL.plist"
PYTHON_BIN=$(command -v python3)
INSIGHTS_BACKFILL_INTERVAL_SECONDS=$(PYTHONPATH="$PROJECT_ROOT/src" "$PYTHON_BIN" -c 'from feishu_archive.config import DEFAULT_INSIGHTS_BACKFILL_INTERVAL_SECONDS; print(DEFAULT_INSIGHTS_BACKFILL_INTERVAL_SECONDS)')
USER_DOMAIN="gui/$(id -u)"
STAGING_DIR=""
BACKUP_DIR=""
TEMP_SERVICE_PLIST=""
TEMP_SYNC_PLIST=""
TEMP_WIKI_SYNC_PLIST=""
TEMP_MAIL_SYNC_PLIST=""
TEMP_INSIGHTS_PLIST=""
TEMP_INSIGHTS_BACKFILL_PLIST=""
SERVICES_STOPPED=0
INSTALL_COMPLETE=0

restore_plist() {
  backup_name=$1
  destination=$2
  if [ -f "$BACKUP_DIR/plists/$backup_name" ]; then
    install -m 600 "$BACKUP_DIR/plists/$backup_name" "$destination" || true
  else
    rm -f -- "$destination" || true
  fi
}

rollback_install() {
  if [ "$SERVICES_STOPPED" -ne 1 ] || [ "$INSTALL_COMPLETE" -eq 1 ]; then
    return
  fi
  echo "安装未完成，正在恢复上一版运行时与 LaunchAgent。" >&2
  launchctl bootout "$USER_DOMAIN/$SERVICE_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$SYNC_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$WIKI_SYNC_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$MAIL_SYNC_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$INSIGHTS_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$INSIGHTS_BACKFILL_LABEL" >/dev/null 2>&1 || true
  if [ -d "$BACKUP_DIR/runtime" ]; then
    mkdir -p "$RUNTIME_DIR"
    rsync -a --delete "$BACKUP_DIR/runtime/" "$RUNTIME_DIR/" || true
  fi
  restore_plist "$SERVICE_LABEL.plist" "$SERVICE_PLIST_PATH"
  restore_plist "$SYNC_LABEL.plist" "$SYNC_PLIST_PATH"
  restore_plist "$WIKI_SYNC_LABEL.plist" "$WIKI_SYNC_PLIST_PATH"
  restore_plist "$MAIL_SYNC_LABEL.plist" "$MAIL_SYNC_PLIST_PATH"
  restore_plist "$INSIGHTS_LABEL.plist" "$INSIGHTS_PLIST_PATH"
  restore_plist "$INSIGHTS_BACKFILL_LABEL.plist" "$INSIGHTS_BACKFILL_PLIST_PATH"
  for restored_plist in \
    "$SERVICE_PLIST_PATH" \
    "$SYNC_PLIST_PATH" \
    "$WIKI_SYNC_PLIST_PATH" \
    "$MAIL_SYNC_PLIST_PATH" \
    "$INSIGHTS_PLIST_PATH" \
    "$INSIGHTS_BACKFILL_PLIST_PATH"
  do
    if [ -f "$restored_plist" ]; then
      launchctl bootstrap "$USER_DOMAIN" "$restored_plist" >/dev/null 2>&1 || true
    fi
  done
}

cleanup() {
  exit_status=$?
  trap - EXIT
  rollback_install
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf -- "$STAGING_DIR"
  fi
  if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf -- "$BACKUP_DIR"
  fi
  for temporary_file in \
    "$TEMP_SERVICE_PLIST" \
    "$TEMP_SYNC_PLIST" \
    "$TEMP_WIKI_SYNC_PLIST" \
    "$TEMP_MAIL_SYNC_PLIST" \
    "$TEMP_INSIGHTS_PLIST" \
    "$TEMP_INSIGHTS_BACKFILL_PLIST"
  do
    if [ -n "$temporary_file" ]; then
      rm -f -- "$temporary_file"
    fi
  done
  exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Feishu Archive 需要 Python 3.11 或更高版本。" >&2
  exit 1
fi

mkdir -p "$ARCHIVE_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$ARCHIVE_DIR" "$LOG_DIR"

# Build and exercise the exact candidate runtime before stopping the working
# reader. This validates the live Mail schema/FTS and reader.secret boundary;
# any failure leaves the existing chat/wiki service running.
STAGING_DIR=$(mktemp -d "$ARCHIVE_DIR/.runtime-stage.XXXXXX")
mkdir -p "$STAGING_DIR/bin" "$STAGING_DIR/src"
chmod 700 "$STAGING_DIR"
rsync -a --delete "$PROJECT_ROOT/src/" "$STAGING_DIR/src/"
install -m 755 "$PROJECT_ROOT/bin/feishu-archive" "$STAGING_DIR/bin/feishu-archive"
install -m 644 "$PROJECT_ROOT/pyproject.toml" "$STAGING_DIR/pyproject.toml"
"$STAGING_DIR/bin/feishu-archive" --archive-dir "$ARCHIVE_DIR" mail-preflight

# Keep a recoverable copy of the currently running code and agent definitions.
# It is removed after the new reader passes its health check.
BACKUP_DIR=$(mktemp -d "$ARCHIVE_DIR/.runtime-backup.XXXXXX")
mkdir -p "$BACKUP_DIR/plists"
chmod 700 "$BACKUP_DIR" "$BACKUP_DIR/plists"
if [ -d "$RUNTIME_DIR" ]; then
  mkdir -p "$BACKUP_DIR/runtime"
  rsync -a "$RUNTIME_DIR/" "$BACKUP_DIR/runtime/"
fi
for current_plist in \
  "$SERVICE_PLIST_PATH" \
  "$SYNC_PLIST_PATH" \
  "$WIKI_SYNC_PLIST_PATH" \
  "$MAIL_SYNC_PLIST_PATH" \
  "$INSIGHTS_PLIST_PATH" \
  "$INSIGHTS_BACKFILL_PLIST_PATH"
do
  if [ -f "$current_plist" ]; then
    install -m 600 "$current_plist" "$BACKUP_DIR/plists/$(basename "$current_plist")"
  fi
done

# Only after the candidate has passed Mail preflight may runtime replacement
# interrupt the existing services.
SERVICES_STOPPED=1
launchctl bootout "$USER_DOMAIN/$SERVICE_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/$SYNC_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/$WIKI_SYNC_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/$MAIL_SYNC_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/$INSIGHTS_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/$INSIGHTS_BACKFILL_LABEL" >/dev/null 2>&1 || true

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
rsync -a --delete "$STAGING_DIR/" "$RUNTIME_DIR/"

# wiki-rebuild initializes only the mandatory chat/wiki database. Mail was
# already initialized and validated above, before the old reader was stopped.
"$RUNTIME_DIR/bin/feishu-archive" --archive-dir "$ARCHIVE_DIR" wiki-rebuild
chmod 600 "$ARCHIVE_DIR/archive.sqlite3"
if [ -f "$ARCHIVE_DIR/mail.sqlite3" ]; then
  chmod 600 "$ARCHIVE_DIR/mail.sqlite3"
fi
"$RUNTIME_DIR/bin/feishu-archive" --archive-dir "$ARCHIVE_DIR" insights-status >/dev/null
chmod 600 "$ARCHIVE_DIR/insights.sqlite3"

TEMP_SERVICE_PLIST=$(mktemp)
TEMP_SYNC_PLIST=$(mktemp)
TEMP_WIKI_SYNC_PLIST=$(mktemp)
TEMP_MAIL_SYNC_PLIST=$(mktemp)
TEMP_INSIGHTS_PLIST=$(mktemp)
TEMP_INSIGHTS_BACKFILL_PLIST=$(mktemp)
rm -f "$TEMP_SERVICE_PLIST" "$TEMP_SYNC_PLIST" "$TEMP_WIKI_SYNC_PLIST" "$TEMP_MAIL_SYNC_PLIST" "$TEMP_INSIGHTS_PLIST" "$TEMP_INSIGHTS_BACKFILL_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $SERVICE_LABEL" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string serve" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:4 string --host" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:5 string 127.0.0.1" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:6 string --port" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:7 string 8765" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :KeepAlive dict" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :KeepAlive:SuccessfulExit bool false" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Interactive" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 5" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/service.log" "$TEMP_SERVICE_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/service.error.log" "$TEMP_SERVICE_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $SYNC_LABEL" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string scheduled-sync" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:4 string --days" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:5 string 2" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval dict" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Hour integer 3" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Minute integer 30" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 60" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/sync.log" "$TEMP_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/sync.error.log" "$TEMP_SYNC_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $WIKI_SYNC_LABEL" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string wiki-scheduled-sync" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval dict" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Hour integer 3" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Minute integer 45" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 60" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/wiki-sync.log" "$TEMP_WIKI_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/wiki-sync.error.log" "$TEMP_WIKI_SYNC_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $MAIL_SYNC_LABEL" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string mail-scheduled-sync" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval dict" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Hour integer 4" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:Minute integer 0" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 60" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/mail-sync.log" "$TEMP_MAIL_SYNC_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/mail-sync.error.log" "$TEMP_MAIL_SYNC_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $INSIGHTS_LABEL" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string insights-run" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:4 string --scheduled" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval array" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:0 dict" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:0:Hour integer 4" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:0:Minute integer 30" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:1 dict" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:1:Hour integer 5" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:1:Minute integer 0" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:2 dict" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:2:Hour integer 5" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartCalendarInterval:2:Minute integer 30" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 60" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/insights.log" "$TEMP_INSIGHTS_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/insights.error.log" "$TEMP_INSIGHTS_PLIST"

/usr/libexec/PlistBuddy -c "Add :Label string $INSIGHTS_BACKFILL_LABEL" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $RUNTIME_DIR/bin/feishu-archive" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --archive-dir" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $ARCHIVE_DIR" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string insights-backfill-step" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:4 string --scheduled" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $RUNTIME_DIR" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATH string $(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:FEISHU_ARCHIVE_PYTHON string $PYTHON_BIN" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :StartInterval integer $INSIGHTS_BACKFILL_INTERVAL_SECONDS" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer $INSIGHTS_BACKFILL_INTERVAL_SECONDS" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :Umask integer 63" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string $LOG_DIR/insights-backfill.log" "$TEMP_INSIGHTS_BACKFILL_PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $LOG_DIR/insights-backfill.error.log" "$TEMP_INSIGHTS_BACKFILL_PLIST"

plutil -lint "$TEMP_SERVICE_PLIST" >/dev/null
plutil -lint "$TEMP_SYNC_PLIST" >/dev/null
plutil -lint "$TEMP_WIKI_SYNC_PLIST" >/dev/null
plutil -lint "$TEMP_MAIL_SYNC_PLIST" >/dev/null
plutil -lint "$TEMP_INSIGHTS_PLIST" >/dev/null
plutil -lint "$TEMP_INSIGHTS_BACKFILL_PLIST" >/dev/null
install -m 600 "$TEMP_SERVICE_PLIST" "$SERVICE_PLIST_PATH"
install -m 600 "$TEMP_SYNC_PLIST" "$SYNC_PLIST_PATH"
install -m 600 "$TEMP_WIKI_SYNC_PLIST" "$WIKI_SYNC_PLIST_PATH"
install -m 600 "$TEMP_MAIL_SYNC_PLIST" "$MAIL_SYNC_PLIST_PATH"
install -m 600 "$TEMP_INSIGHTS_PLIST" "$INSIGHTS_PLIST_PATH"
install -m 600 "$TEMP_INSIGHTS_BACKFILL_PLIST" "$INSIGHTS_BACKFILL_PLIST_PATH"

launchctl enable "$USER_DOMAIN/$SERVICE_LABEL"
launchctl enable "$USER_DOMAIN/$SYNC_LABEL"
launchctl enable "$USER_DOMAIN/$WIKI_SYNC_LABEL"
launchctl enable "$USER_DOMAIN/$MAIL_SYNC_LABEL"
launchctl enable "$USER_DOMAIN/$INSIGHTS_LABEL"
launchctl enable "$USER_DOMAIN/$INSIGHTS_BACKFILL_LABEL"
launchctl bootstrap "$USER_DOMAIN" "$SERVICE_PLIST_PATH"
launchctl bootstrap "$USER_DOMAIN" "$SYNC_PLIST_PATH"
launchctl bootstrap "$USER_DOMAIN" "$WIKI_SYNC_PLIST_PATH"
launchctl bootstrap "$USER_DOMAIN" "$MAIL_SYNC_PLIST_PATH"
launchctl bootstrap "$USER_DOMAIN" "$INSIGHTS_PLIST_PATH"

ATTEMPT=0
while [ "$ATTEMPT" -lt 20 ]; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8765/api/status >/dev/null \
    && launchctl print "$USER_DOMAIN/$SERVICE_LABEL" >/dev/null 2>&1 \
    && launchctl print "$USER_DOMAIN/$SYNC_LABEL" >/dev/null 2>&1 \
    && launchctl print "$USER_DOMAIN/$WIKI_SYNC_LABEL" >/dev/null 2>&1 \
    && launchctl print "$USER_DOMAIN/$MAIL_SYNC_LABEL" >/dev/null 2>&1 \
    && launchctl print "$USER_DOMAIN/$INSIGHTS_LABEL" >/dev/null 2>&1; then
    # Start the mutating backfill agent only after the reader and all existing
    # scheduled lanes have passed deployment health checks. Until this point a
    # failed install can roll back code/plists without new-version data writes.
    launchctl bootstrap "$USER_DOMAIN" "$INSIGHTS_BACKFILL_PLIST_PATH"
    launchctl print "$USER_DOMAIN/$INSIGHTS_BACKFILL_LABEL" >/dev/null 2>&1
    launchctl kickstart "$USER_DOMAIN/$INSIGHTS_BACKFILL_LABEL"
    INSTALL_COMPLETE=1
    echo "Feishu Archive 已部署：http://127.0.0.1:8765"
    echo "档案目录：$ARCHIVE_DIR"
    echo "阅读器服务：$SERVICE_LABEL"
    echo "每日同步：${SYNC_LABEL}（每天 03:30）"
    echo "知识库同步：${WIKI_SYNC_LABEL}（每天 03:45）"
    echo "邮箱同步：${MAIL_SYNC_LABEL}（每天 04:00；未授权时安全跳过，不影响其他通道）"
    echo "每日洞察：${INSIGHTS_LABEL}（每天 04:30，失败时 05:00/05:30 断点重试）"
    echo "历史洞察回填：${INSIGHTS_BACKFILL_LABEL}（全天候，启动时及每 ${INSIGHTS_BACKFILL_INTERVAL_SECONDS} 秒尝试一个受控步骤）"
    exit 0
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 0.5
done

echo "服务未在预期时间内就绪，请检查：$LOG_DIR/service.error.log" >&2
exit 1
