#!/bin/sh
set -eu

LABEL="com.fermiwang.feishu-archive"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
USER_DOMAIN="gui/$(id -u)"

launchctl bootout "$USER_DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Feishu Archive 后台服务已移除。"
echo "档案数据仍保留在：$HOME/Library/Application Support/Feishu Archive"
