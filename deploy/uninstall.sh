#!/usr/bin/env bash
# 移除服務。加上 --purge 才會刪除資料庫與設定（~/twradar-data）
set -u
if [ "$(uname -s)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)/tw.radar" 2>/dev/null; rm -f "$HOME/Library/LaunchAgents/tw.radar.plist"
else
  systemctl --user disable --now twradar.service 2>/dev/null; rm -f "$HOME/.config/systemd/user/twradar.service"
  ( crontab -l 2>/dev/null | grep -v 'twradar serve' ) | crontab - 2>/dev/null; pkill -f "twradar serve" 2>/dev/null
fi
rm -rf "$HOME/twradar-app"
[ "${1:-}" = "--purge" ] && rm -rf "${TWRADAR_HOME:-$HOME/twradar-data}" && echo "已刪除資料"
echo "已移除服務"
