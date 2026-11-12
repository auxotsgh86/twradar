#!/usr/bin/env bash
# 右側加碼雷達 一鍵安裝（macOS / Linux）
# 用法：bash deploy/install.sh        重新執行即為更新（資料與 token 會保留）
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/twradar-app"
DATA="${TWRADAR_HOME:-$HOME/twradar-data}"
PORT="${TWRADAR_PORT:-8765}"
OS="$(uname -s)"
say(){ printf "\n\033[1;34m▶ %s\033[0m\n" "$*"; }
die(){ printf "\n\033[1;31m✖ %s\033[0m\n" "$*"; exit 1; }

say "1/5 複製程式到 $APP"
mkdir -p "$APP" "$DATA"
rm -rf "$APP/twradar"
cp -R "$SRC/twradar" "$SRC/pyproject.toml" "$APP/"
cp -R "$SRC/deploy" "$APP/"

say "2/5 準備 Python（使用 uv 自動安裝，不會動到系統 Python）"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 || die "找不到 curl，請先安裝 curl"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv 安裝失敗"
PREF="system"; [ "$OS" = "Darwin" ] && PREF="only-managed"   # Mac 不碰系統內建的 python3 佔位程式
rm -rf "$APP/.venv"
uv venv --quiet --python-preference "$PREF" --python ">=3.10" "$APP/.venv" || die "無法建立 Python 環境"
PY="$APP/.venv/bin/python"
uv pip install --quiet --python "$PY" certifi || echo "（certifi 安裝失敗，略過）"

say "3/5 建立設定與資料庫（$DATA）"
export TWRADAR_HOME="$DATA"
"$PY" -c "import sys; sys.path.insert(0,'$APP'); from twradar.config import load_config; c=load_config(); c.port=$PORT; from twradar.config import save_config; save_config(c)"

say "4/5 設定開機自動執行"
if [ "$OS" = "Darwin" ]; then
  PL="$HOME/Library/LaunchAgents/tw.radar.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>tw.radar</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>-m</string><string>twradar</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict><key>TWRADAR_HOME</key><string>$DATA</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA/service.log</string>
  <key>StandardErrorPath</key><string>$DATA/service.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/tw.radar" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PL"
  launchctl kickstart -k "gui/$(id -u)/tw.radar" 2>/dev/null || true
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UD="$HOME/.config/systemd/user"; mkdir -p "$UD"
  cat > "$UD/twradar.service" <<UNIT
[Unit]
Description=右側加碼雷達 twradar
After=network-online.target
[Service]
Environment=TWRADAR_HOME=$DATA
WorkingDirectory=$APP
ExecStart=$PY -m twradar serve
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now twradar.service
  systemctl --user restart twradar.service
  # 讓服務在沒有登入時也持續執行
  loginctl enable-linger "$USER" 2>/dev/null || sudo -n loginctl enable-linger "$USER" 2>/dev/null \
    || echo "⚠️  請執行一次：sudo loginctl enable-linger $USER（讓電腦登出後仍持續執行）"
else
  if command -v crontab >/dev/null 2>&1; then
    echo "沒有 systemd，改用 cron @reboot 方式"
    ( crontab -l 2>/dev/null | grep -v 'twradar serve' || true; echo "@reboot cd $APP && TWRADAR_HOME=$DATA nohup $PY -m twradar serve >> $DATA/service.log 2>&1 &" ) | crontab - || true
  else
    echo "⚠️  沒有 systemd 也沒有 cron：這次先直接啟動，重開機後需重新執行安裝"
  fi
  pkill -f "twradar serve" 2>/dev/null || true
  (cd "$APP" && TWRADAR_HOME="$DATA" nohup "$PY" -m twradar serve >> "$DATA/service.log" 2>&1 &)
fi

say "5/5 確認服務啟動"
for i in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then OK=1; break; fi; sleep 1
done
[ "${OK:-0}" = 1 ] || die "服務沒有啟動，請查看 $DATA/service.log"

INFO="$(cd "$APP" && TWRADAR_HOME="$DATA" "$PY" -m twradar info)"
DESK="$HOME/Desktop"; [ -d "$DESK" ] || DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME"
printf "%s\n\n第一次啟動會在背景補抓約 100 個交易日的歷史資料（約 1～2 小時），完成前月線、季線可能不完整。\n請保持電腦開機、不要進入睡眠。\n" "$INFO" > "$DESK/右側加碼雷達-連線資訊.txt"
echo "$INFO"
echo "✅ 安裝完成！連線資訊已存到：$DESK/右側加碼雷達-連線資訊.txt"
URL="http://localhost:$PORT/#t=$(sed -n 's/^API token：//p' <<<"$INFO")"
if [ "$OS" = "Darwin" ]; then open "$URL" || true; fi
