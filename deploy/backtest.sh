#!/usr/bin/env bash
# 右側加碼雷達：回溯分析一鍵執行（macOS / Linux）
#
#   ./deploy/backtest.sh                 補約兩年資料（第一次約 8～12 小時）→ 回溯分析 → 打開報表
#   ./deploy/backtest.sh --skip-backfill 資料已補好，只重跑回溯分析
#   ./deploy/backtest.sh --days 300      只補 300 個交易日
#   ./deploy/backtest.sh -- --grid stopLossPct=8,10,12   「--」之後的參數直接交給 twradar backtest
#
# 中斷（Ctrl+C、關機）後重新執行同一個指令，會從斷點接著補。
# 資料放在獨立的資料夾（預設 ~/twradar-backtest），不會影響正在運作的雷達。
set -euo pipefail

cd "$(dirname "$0")/.."
DATA="${TWRADAR_BACKTEST_HOME:-$HOME/twradar-backtest}"
DAYS=560
INTERVAL=""
SKIP=0
GRID=(--grid profitTargetPct=8,10,15 --grid stopLossPct=8,10,12)
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-backfill) SKIP=1 ;;
    --days) DAYS="$2"; shift ;;
    --interval) INTERVAL="$2"; shift ;;
    --data) DATA="$2"; shift ;;
    --) shift; EXTRA=("$@"); GRID=(); break ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "不認識的參數：$1（用 --help 看說明）"; exit 1 ;;
  esac
  shift
done

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "需要 Python 3.10 以上（macOS 可用 brew install python 安裝）"; exit 1; }

export TWRADAR_HOME="$DATA"
mkdir -p "$DATA"
echo "▶ 程式：$(pwd)"
echo "▶ 資料：$DATA"
echo "▶ Python：$($PY --version)"

# 第一次：沿用正在運作的雷達的觀察清單與規則（如果有）
LIVE="${TWRADAR_LIVE_HOME:-$HOME/twradar-data}"
if [ ! -f "$DATA/settings.json" ] && [ -f "$LIVE/settings.json" ]; then
  cp "$LIVE/settings.json" "$DATA/settings.json"
  echo "▶ 已複製 $LIVE/settings.json 的觀察清單與規則"
fi

if [ -n "$INTERVAL" ]; then
  "$PY" - "$INTERVAL" <<'PYEOF'
import sys
from twradar.config import load_config, save_config
cfg = load_config(); cfg.request_interval = float(sys.argv[1]); save_config(cfg)
print(f"▶ 請求間隔改為 {cfg.request_interval} 秒（太短可能被交易所暫時封鎖）")
PYEOF
fi

# 不讓電腦睡著（macOS）
KEEP=()
if command -v caffeinate >/dev/null 2>&1; then KEEP=(caffeinate -i); fi

if [ "$SKIP" = 0 ]; then
  echo
  echo "▶ 1/3 檢查資料來源"
  if ! "$PY" -m twradar doctor; then
    echo "⚠ 部分資料來源檢查失敗。如果只是「尚未公布／無資料」（例如測試日剛好休市）可以忽略；"
    echo "  補抓時連續 3 天全部失敗會自動停止。Ctrl+C 可中止。10 秒後繼續…"
    sleep 10
  fi
  echo
  echo "▶ 2/3 補抓 $DAYS 個交易日的全市場資料（可隨時 Ctrl+C，之後重跑會接著補）"
  ${KEEP[@]+"${KEEP[@]}"} "$PY" -m twradar backfill --days "$DAYS"
else
  echo "▶ 略過補抓"
fi

echo
echo "▶ 3/3 回溯分析"
OUT="$DATA/backtest/$(date +%Y%m%d-%H%M%S)"
${KEEP[@]+"${KEEP[@]}"} "$PY" -m twradar backtest --out "$OUT" ${GRID[@]+"${GRID[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}

REPORT="$OUT/report.html"
if [ -f "$REPORT" ]; then
  echo
  echo "✅ 完成。請把這兩個檔案傳給維護者：
   $REPORT
   $OUT/result.json"
  if [ "$(uname)" = Darwin ]; then open "$REPORT"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$REPORT" >/dev/null 2>&1 || true; fi
fi
