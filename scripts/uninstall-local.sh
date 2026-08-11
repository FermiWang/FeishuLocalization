#!/bin/sh
set -eu

SERVICE_LABEL="com.fermiwang.feishu-archive"
SYNC_LABEL="com.fermiwang.feishu-archive-sync"
WIKI_SYNC_LABEL="com.fermiwang.feishu-archive-wiki-sync"
MAIL_SYNC_LABEL="com.fermiwang.feishu-archive-mail-sync"
USER_DOMAIN="gui/$(id -u)"

for LABEL in "$SERVICE_LABEL" "$SYNC_LABEL" "$WIKI_SYNC_LABEL" "$MAIL_SYNC_LABEL"; do
  launchctl bootout "$USER_DOMAIN/$LABEL" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
done

echo "Feishu Archive 阅读器、消息同步、知识库同步和邮箱同步服务已移除。"
echo "档案数据仍保留在：$HOME/Library/Application Support/Feishu Archive"
