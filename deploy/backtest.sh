#!/usr/bin/env bash
# 右側加碼雷達：回溯分析一鍵執行（macOS / Linux）
#
#   ./deploy/backtest.sh                 補約兩年資料（第一次約 8～12 小時）→ 回溯分析 → 打開報表
#   ./deploy/backtest.sh --skip-backfill 資料已補好，只重跑回溯分析
#   ./deploy/backtest.sh --days 300      只補 300 個交易日
#   ./deploy/backtest.sh -- --grid stopLossPct=8,10,12   「--」之後的參數直接交給 twradar backtest
#
#   ./deploy/backtest.sh --separate      不共用雷達的資料，改用獨立資料夾 ~/twradar-backtest
#
# 資料夾：
#   - 這台電腦已安裝雷達（有 ~/twradar-data）→ 共用雷達的資料庫，抓一次、晨報與回溯都能用。
#     雷達正在執行時，補抓交給雷達的背景工作（與每日排程排隊，不會同時對交易所發請求），本程式只顯示進度。
#   - 沒有安裝雷達，或加 --separate → 用獨立資料夾 ~/twradar-backtest。
# 中斷（Ctrl+C、關機）後重新執行同一個指令，會從斷點接著補。
set -euo pipefail

cd "$(dirname "$0")/.."
LIVE="${TWRADAR_LIVE_HOME:-$HOME/twradar-data}"
DATA="${TWRADAR_BACKTEST_HOME:-}"
SEPARATE=0
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
    --separate) SEPARATE=1 ;;
    --) shift; EXTRA=("$@"); GRID=(); break ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "不認識的參數：$1（用 --help 看說明）"; exit 1 ;;
  esac
  shift
done

SHARED=0
if [ -z "$DATA" ]; then
  if [ "$SEPARATE" = 0 ] && [ -f "$LIVE/config.json" ]; then DATA="$LIVE"; SHARED=1; else DATA="$HOME/twradar-backtest"; fi
fi

PY=""
for c in ./.venv/bin/python python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "需要 Python 3.10 以上（macOS 可用 brew install python 安裝）"; exit 1; }

export TWRADAR_HOME="$DATA"
mkdir -p "$DATA"
echo "▶ 程式：$(pwd)"
echo "▶ 資料：$DATA"
echo "▶ Python：$($PY --version)"

# 雷達服務是否正在執行（看本機 API 有沒有回應）
SERVICE=0
if [ "$SHARED" = 1 ] && "$PY" - <<'PYEOF' >/dev/null 2>&1
import json, urllib.request
from twradar.config import load_config
cfg = load_config()
urllib.request.urlopen(f"http://127.0.0.1:{cfg.port}/api/v1/health", timeout=5).read()
PYEOF
then SERVICE=1; fi
if [ "$SHARED" = 1 ]; then
  echo "▶ 共用雷達的資料（$([ "$SERVICE" = 1 ] && echo '雷達執行中，補抓交給雷達處理' || echo '雷達沒有在執行')）"
fi

# 第一次（獨立資料夾）：沿用雷達的觀察清單與規則
if [ "$SHARED" = 0 ] && [ ! -f "$DATA/settings.json" ] && [ -f "$LIVE/settings.json" ]; then
  cp "$LIVE/settings.json" "$DATA/settings.json"
  echo "▶ 已複製 $LIVE/settings.json 的觀察清單與規則"
fi

if [ -n "$INTERVAL" ] && [ "$SHARED" = 1 ]; then
  echo "⚠ 共用雷達資料時不能改請求間隔（會影響雷達本身），忽略 --interval"
  INTERVAL=""
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
  if [ "$SERVICE" = 1 ]; then
    echo "  雷達執行中，略過（可在網頁的「設定與狀態」看最近的抓取結果）"
  elif ! "$PY" -m twradar doctor; then
    echo "⚠ 部分資料來源檢查失敗。如果只是「尚未公布／無資料」（例如測試日剛好休市）可以忽略；"
    echo "  補抓時連續 3 天全部失敗會自動停止。Ctrl+C 可中止。10 秒後繼續…"
    sleep 10
  fi
  echo
  if [ "$SERVICE" = 1 ]; then
    echo "▶ 2/3 請雷達補抓 $DAYS 個交易日的全市場資料（Ctrl+C 只會停止顯示進度，雷達會繼續補）"
    "$PY" - "$DAYS" <<'PYEOF' || exit 1
import json, os, sys, time, urllib.request
from twradar.config import load_config
cfg = load_config()
base, days = f"http://127.0.0.1:{cfg.port}/api/v1", int(sys.argv[1])
hdr = {"Authorization": "Bearer " + cfg.admin_token, "Content-Type": "application/json"}

def call(path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=hdr, method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

since = call("/status")["now"]
r = call("/backfill", {"days": days})
print("  已交給雷達的背景工作" + ("" if r.get("queued") else "（同樣的補抓已在進行或排隊中）"), flush=True)
t0, last = None, None
try:
    while True:
        time.sleep(int(os.environ.get("TWRADAR_POLL", 30)))
        stt = call("/status")
        bf = stt.get("backfill") or {}
        if not (bf.get("target") == days and (bf.get("started_at") or "") >= since[:19]):
            busy = (stt.get("worker") or {}).get("current")
            msg = f"  等待雷達開始補抓（目前在做：{busy}）" if busy else "  等待雷達開始補抓…"
            if msg != last:
                print(msg, flush=True); last = msg
            continue
        t0 = t0 or time.time()
        line = f"  {bf.get('done', 0)}/{bf.get('target', days)} 天" + (f"，正在抓 {bf['current']}" if bf.get("current") else "")
        if bf.get("done") and bf.get("status") != "done":
            eta = (time.time() - t0) / max(bf["done"], 1) * (bf["target"] - bf["done"]) / 60
            line += f"（預估還要 {eta:.0f} 分鐘；已存在的日期會跳過，實際可能更快）"
        if line != last:
            print(line, flush=True); last = line
        if bf.get("status") == "done":
            if bf.get("error"):
                print("  ⚠ " + bf["error"]); sys.exit(1)
            break
except KeyboardInterrupt:
    print("\n停止顯示進度。雷達會繼續補抓；之後執行 ./deploy/backtest.sh --skip-backfill 再做分析。")
    sys.exit(130)
PYEOF
  else
    echo "▶ 2/3 補抓 $DAYS 個交易日的全市場資料（可隨時 Ctrl+C，之後重跑會接著補）"
    ${KEEP[@]+"${KEEP[@]}"} "$PY" -m twradar backfill --days "$DAYS"
  fi
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
