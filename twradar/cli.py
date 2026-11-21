"""指令列：python -m twradar <指令>

  serve                 啟動網頁、API 與每日排程（安裝後由系統服務自動執行）
  crawl [--date D] [--final]   立即抓取某一天
  backfill [--days N]   補抓歷史資料
  analyze [--date D]    重新分析
  doctor [--date D]     測試各資料來源是否可以連線、格式是否正確
  probe [--date D]      列出櫃買中心各網址實際回傳內容（給維護者除錯用）
  info                  顯示網址、token 與初始管理密碼
  backtest              回溯分析（先用 backfill --days 560 補約兩年資料）
  admin reset-password  忘記管理密碼時，在主機上產生新的密碼
  admin set-password    在主機上設定管理密碼
  admin rotate-token read|admin   重設 token
  show CODE [--date D]  顯示某檔某日的指標與訊號（核對數字用）
  watchlist             列出觀察清單
  import FILE           匯入「右側加碼雷達」輸出的 JSON 觀察清單
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from logging.handlers import RotatingFileHandler

from . import __version__
from .analysis import run_analysis
from .config import load_config, home_dir
from .db import DB
from .dates import norm_date, prev_weekday
from .jobs import Crawler, today
from .net import FetchError, Fetcher
from .settings import SettingsStore, open_settings, stock
from .providers import build
from .providers.common import dates_in, tables

def setup_logging(cfg) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]


def open_db(cfg) -> DB:
    return DB(cfg.db_path)


def open_all(cfg) -> tuple[DB, SettingsStore]:
    """開啟資料庫與設定；第一次啟動 v0.2 時會把 v0.1 的觀察清單與門檻搬到 settings.json。"""
    db = open_db(cfg)
    return db, open_settings(cfg, db)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def info_text(cfg, secrets: bool = True) -> str:
    ip = lan_ip()
    if not secrets:  # 服務啟動時寫進紀錄檔的版本：不含管理密碼與管理 token
        return (f"twradar {__version__}\n資料夾：{home_dir()}\n本機網址：http://localhost:{cfg.port}/\n"
                f"區網網址：http://{ip}:{cfg.port}/\n連線資訊：執行 twradar info\n")
    pw = (f"初始管理密碼（登入後請到「設定 → 權限」改成自己的密碼）：{cfg.admin_initial_password}"
          if cfg.admin_initial_password else "管理密碼：已自訂（忘記時在這台電腦執行 twradar admin reset-password）")
    return (f"twradar {__version__}\n"
            f"資料夾：{home_dir()}\n"
            f"本機網址：http://localhost:{cfg.port}/#t={cfg.read_token}\n"
            f"區網網址：http://{ip}:{cfg.port}/#t={cfg.read_token}\n"
            f"{pw}\n"
            f"唯讀 token（給晨報）：{cfg.read_token}\n"
            f"管理 token（給管理腳本，只能放在 Authorization 標頭）：{cfg.admin_token}\n"
            f"API 範例：http://{ip}:{cfg.port}/api/v1/latest?token={cfg.read_token}\n")


def doctor(cfg, date: str | None = None, transport=None, settings: SettingsStore | None = None) -> int:
    f = transport or Fetcher(max(3.0, cfg.request_interval), cfg.user_agent)
    d = norm_date(date) if date else prev_weekday(today())
    primary, fallback, opts = (settings or open_all(cfg)[1]).source_plan(cfg.env)
    print(f"測試資料日：{d}（若遇到休市，請改用 --date 指定）")
    print(f"資料源：主要 {', '.join(primary)}；備援 {', '.join(fallback) or '（無）'}\n")
    bad = 0
    for pv in build(primary + fallback, f, opts):
        for label, fn in pv.doctor(d):
            try:
                print(f"  ✅ {label}：{fn()}")
            except FetchError as e:
                bad += 1
                print(f"  ❌ {label}：{e}" + (f"\n       {e.url}" if e.url else ""))
            except Exception as e:
                bad += 1
                print(f"  ❌ {label}：格式解析錯誤 {e!r}")
    print("\n全部正常" if not bad else f"\n{bad} 項失敗。請執行 `python -m twradar probe` 並把結果傳給維護者。")
    return 1 if bad else 0


def probe(cfg, date: str | None = None) -> None:
    """列出櫃買中心各候選網址實際回傳的內容摘要，協助修正格式。"""
    f = Fetcher(max(3.0, cfg.request_interval), cfg.user_agent)
    d = norm_date(date) if date else prev_weekday(today())
    _, _, opts = open_all(cfg)[1].source_plan(cfg.env)
    print(f"要求日期：{d}\n")
    for u in build(["tpex"], f, opts)[0].quote_urls(d):
        print("■", u)
        try:
            js = f.get_json(u, retries=0)
        except FetchError as e:
            print(f"   失敗：{e}\n")
            continue
        if isinstance(js, dict):
            print("   頂層欄位：", {k: (v if isinstance(v, (str, int, float)) and len(str(v)) < 60 else type(v).__name__)
                                for k, v in js.items()})
        print("   回應中的日期：", dates_in(js) or "（無）")
        for i, (title, fields, rows) in enumerate(tables(js)):
            print(f"   表格 {i}：{str(title)[:50]}｜{len(rows)} 列")
            print("     欄位：", fields)
            if rows:
                print("     第一列：", rows[0])
        print()


def holders_history_codes(db, st, which: str, weeks: list[str]) -> list[str]:
    """回補的股票：觀察清單在前；universe 再加上這段期間每季回溯股票池（大型、中型、上櫃）。"""
    from . import backtest as B
    wl = [w["code"] for w in st.watchlist()]
    if which == "watchlist":
        return wl
    if which != "universe":
        return [c.strip().upper() for c in which.split(",") if c.strip()]
    codes = dict.fromkeys(wl)
    trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? ORDER BY date", (min(weeks),))]
    starts, seen = [], set()
    for d in trading:
        qk = (d[:4], (int(d[4:6]) - 1) // 3)
        if qk not in seen:
            seen.add(qk)
            starts.append(d)
    for d in starts:
        for c in B.rank_universe(db, d, B.BTConfig("", "", "")):
            codes.setdefault(c)
    return list(codes)


def run_macro_history(cfg, args) -> int:
    """外資台指期淨未平倉、美元兌新台幣（期交所，每段一年）與市場廣度（由已有行情計算）。"""
    from datetime import timedelta

    from .dates import to_date
    from .macro import Macro, compute_breadth
    from .net import Fetcher
    from .providers.taifex import Taifex
    db, st = open_all(cfg)
    if not args.no_fetch:
        first = db.one("SELECT MIN(date) AS d FROM market WHERE taiex IS NOT NULL")["d"]
        start = args.start or first or "20240101"
        from .config import now_tw
        last_day = db.one("SELECT MAX(date) AS d FROM market WHERE taiex IS NOT NULL")["d"]
        end = args.end or now_tw().strftime("%Y%m%d")
        if last_day and end > last_day:   # 期交所查詢的結束日超過最新資料日會回傳錯誤頁
            end = last_day
        opts = st.source_plan(cfg.env)[2].get("taifex") or {}
        tf = Taifex(Fetcher(max(cfg.request_interval, 3.0), cfg.user_agent), **({"base": opts["base"]} if opts.get("base") else {}))
        a = to_date(start)
        while a.strftime("%Y%m%d") <= end:
            b = min(a + timedelta(days=364), to_date(end))
            s, e = a.strftime("%Y%m%d"), b.strftime("%Y%m%d")
            rows, errs = tf.fetch(s, e)
            db.put_macro(rows)
            fut = sum(1 for r in rows if "fut_foreign" in r)
            fx = sum(1 for r in rows if "usd_twd" in r)
            print(f"{s}～{e}：外資期貨 {fut} 天、匯率 {fx} 天" + ("；" + "；".join(errs) if errs else ""), flush=True)
            a = b + timedelta(days=1)
    n = compute_breadth(db)
    print(f"市場廣度：新算 {n} 天（除權息還原；短期＝站上月線、長期＝站上 200 日線）")
    r = db.one("SELECT COUNT(fut_foreign) AS f, COUNT(usd_twd) AS x, COUNT(breadth20) AS b, COUNT(breadth200) AS b2, "
               "MIN(date) AS a, MAX(date) AS z FROM macro")
    print(f"資料庫：外資期貨 {r['f']} 天、匯率 {r['x']} 天、短期廣度 {r['b']} 天、長期廣度 {r['b2']} 天（{r['a']}～{r['z']}）")
    last = db.one("SELECT MAX(date) AS d FROM market WHERE taiex IS NOT NULL")["d"]
    if last:
        from .analysis import DEFAULT_RULES
        from .macro import foreign_signals
        f = Macro(db, last, (to_date(last) - timedelta(days=40)).strftime("%Y%m%d")).fields(last)
        hit, have, why = foreign_signals(f, DEFAULT_RULES)
        print(f"{last}：" + "、".join(f"{k}={v}" for k, v in f.items()))
        print(f"外資撤出訊號 {hit}/{have}" + (f"（{'、'.join(why)}）" if why else ""))
    return 0


def run_long_history(cfg, args) -> int:
    """長期歷史（加權指數、0050、加權報酬指數），一個月一個請求，可中斷續跑。"""
    from .config import now_tw
    from .longhist import fetch
    from .net import Fetcher
    db, _ = open_all(cfg)
    end = args.end or now_tw().strftime("%Y%m%d")
    series = tuple(x for x in args.series.split(",") if x)
    print(f"抓 {','.join(series)}：{args.start[:6]}～{end[:6]}，每個請求間隔 {args.interval} 秒", flush=True)
    st = fetch(db, Fetcher(args.interval, cfg.user_agent), args.start, end, series)
    blocked = st.pop("blocked", None)
    for k, v in st.items():
        print(f"{k}：新抓 {v['fetched']} 個月、略過 {v['skipped']}、失敗 {v['failed']}，資料庫共 {v['days']} 天")
    if blocked:
        print(f"\n抓到 {blocked} 時被證交所暫時封鎖，已停止。請等 30～60 分鐘後再執行同一個指令，"
              "已抓的月份會跳過；可以加 --interval 12 放慢。")
        return 2
    return 0


def run_timing_study(cfg, args) -> int:
    """長期歷史上比較「抱 0050、大盤空頭時空手」各種規則與一直抱著。"""
    from .longhist import study
    db, _ = open_all(cfg)
    for asset in args.asset.split(","):
        r = study(db, args.start or "", asset)
        if "error" in r:
            print(r["error"])
            continue
        print(f"\n== {'0050（還原除息，開盤成交）' if asset == '0050' else '加權報酬指數（含息，收盤成交）'}：{r['start']}～{r['end']}，{r['days']} 個交易日")
        print(f"{'規則':<22}{'總報酬':>10}{'年化':>8}{'最大回落':>9}{'進出':>6}{'持有':>7}")
        for k, v in r["rules"].items():
            print(f"{k:<22}{v.get('total', 0):>9}%{v.get('cagr', 0):>7}%{v.get('maxDD', 0):>8}%{v.get('switches', 0):>6}{v.get('exposure', 0):>6}%")
        if args.years:
            ys = sorted(r["rules"]["一直抱著"]["years"])
            names = list(r["rules"])
            print("\n年度報酬（%）：" + "、".join(f"{i}＝{n}" for i, n in enumerate(names)))
            print("年    " + "".join(f"{i:>7}" for i in range(len(names))))
            for y in ys:
                print(y + "  " + "".join(f"{r['rules'][n]['years'].get(y, 0):>7}" for n in names))
    return 0


def run_vector_study(cfg, args) -> int:
    """大盤資金向量（淨方向＋分歧度）的樣本內／外驗證；不影響任何規則。"""
    from .dates import norm_date
    from .vector import render, study
    db, _ = open_all(cfg)
    ws = tuple(int(x) for x in args.window.split(",") if x)
    hs = tuple(int(x) for x in args.horizons.split(",") if x)
    r = study(db, ws, hs, norm_date(args.split) if args.split else None, norm_date(args.start) if args.start else "")
    print(render(r, hs))
    return 0 if "error" not in r else 1


def run_holders_history(cfg, args) -> int:
    import time as _t

    from .net import FetchError
    from .providers.tdcc_history import Session
    db, st = open_all(cfg)
    s = Session(cfg.user_agent, args.interval)
    weeks = s.start()
    if args.weeks:
        weeks = weeks[:args.weeks]
    codes = holders_history_codes(db, st, args.codes, weeks)
    have = {(r["code"], r["date"]) for r in db.q("SELECT DISTINCT code, date FROM holders WHERE level=17")}
    todo = [(c, w) for c in codes for w in weeks if (c, w) not in have]
    total = len(todo)
    print(f"集保歷史回補：{len(codes)} 檔 × {len(weeks)} 週（{min(weeks)}～{max(weeks)}），已有 {len(codes) * len(weeks) - total} 筆，"
          f"待查 {total} 筆；間隔 {args.interval:g} 秒，預估 {total * args.interval / 3600:.1f} 小時", flush=True)
    t0, ok, none, fail = _t.monotonic(), 0, 0, 0
    for i, (c, w) in enumerate(todo, 1):
        try:
            rows = s.query(c, w)
        except FetchError as e:
            fail += 1
            print(f"  失敗：{e}", flush=True)
            if fail >= 20 and fail > ok:
                print("連續失敗太多，先停止；稍後再執行同一個指令會從中斷處接著抓。", flush=True)
                return 1
            continue
        if rows is None:
            none += 1
        else:
            db.put_holders(rows)
            ok += 1
        if i % 25 == 0 or i == total:
            el = _t.monotonic() - t0
            eta = el / i * (total - i)
            print(f"  {i}/{total}（{c} {w}）成功 {ok}、查無 {none}、失敗 {fail}；剩約 {eta / 3600:.1f} 小時", flush=True)
    print(f"完成：成功 {ok}、查無資料 {none}、失敗 {fail}。可中斷後再執行同一個指令補齊。", flush=True)
    return 0


def run_alerts(cfg, args) -> int:
    from datetime import timedelta

    from .dates import norm_date, to_date
    from .net import Fetcher
    from .providers.alerts import Alerts
    db, _ = open_all(cfg)
    d = norm_date(args.date) if args.date else (db.one("SELECT MAX(date) AS d FROM market WHERE taiex IS NOT NULL")["d"])
    if not d:
        print("資料庫還沒有行情資料")
        return 1
    if not args.no_fetch:
        start = (to_date(d) - timedelta(days=40)).strftime("%Y%m%d")
        rows, errs = Alerts(Fetcher(cfg.request_interval, cfg.user_agent)).fetch(start, d)
        db.put_alerts(rows)
        by = {}
        for r in rows:
            by[(r["src"])] = by.get(r["src"], 0) + 1
        print(f"抓取 {start}～{d}：{len(rows)} 筆（{'、'.join(f'{k} {v}' for k, v in by.items()) or '無'}）")
        for e in errs:
            print("  失敗：" + e)
    import re
    a = db.alerts_for(None, d)
    stock = lambda c: re.fullmatch(r"\d{4}", c) is not None   # 一般股票；權證、可轉債等只計數
    mkt = {r["code"]: r["market"] for r in db.q("SELECT code, market FROM alerts WHERE code IN (%s)" % ",".join("?" * len(a)),
                                                tuple(a))} if a else {}
    disp = sorted((c, x) for c, x in a.items() if x.get("disposal"))
    ds = [(c, x) for c, x in disp if stock(c)]
    print(f"\n{d} 處置中：股票 {len(ds)} 檔（另有權證、可轉債等 {len(disp) - len(ds)} 檔）")
    for c, x in ds:
        p = x["disposal"]
        print(f"  {c} {x.get('name') or ''}（{'上櫃' if mkt.get(c) == 'TPEx' else '上市'}）\t{p['start']}～{p['end']}\t"
              f"{p.get('measure') or ''}\t{p.get('reason') or ''}"[:160])
    note = sorted((c, x) for c, x in a.items() if x.get("notice"))
    ns = [(c, x) for c, x in note if stock(c)]
    print(f"\n{d} 當天注意：股票 {len(ns)} 檔（另有權證、可轉債等 {len(note) - len(ns)} 檔）")
    for c, x in ns:
        print(f"  {c} {x.get('name') or ''}（{'上櫃' if mkt.get(c) == 'TPEx' else '上市'}）\t近10日 {x.get('notices10', 0)} 次\t"
              f"{x['notice'].get('reason') or ''}"[:160])
    return 0


def run_scan(cfg, args) -> int:
    from . import scan as SC
    from .dates import norm_date
    db, _ = open_all(cfg)
    try:
        r = SC.latest(db, norm_date(args.date) if args.date else None, args.method, SC.ScanConfig(top=args.top))
    except ValueError as e:
        print(f"錯誤：{e}")
        return 1
    print(f"{r['date']} 全市場掃描（{r['method']}）：流動性合格 {r['liquid']} 檔，趨勢合格 {r['trend']} 檔")
    print("代號\t名稱\t收盤\t20日相對%\t60日相對%\t法人5日%\t5日乖離%\t乖離百分位\t日均額(億)\t分數")
    f = lambda v, d=1: "—" if v is None else f"{v:.{d}f}"
    for x in r["rows"]:
        print(f"{x['code']}\t{x['name']}\t{f(x['close'], 2)}\t{f(x['rs20'])}\t{f(x['rs60'])}\t{f(x['inst5Pct'])}\t"
              f"{f(x['bias5'])}\t{f(x['biasPct'], 0)}\t{f(x['value20'])}\t{f(x['score'])}")
    print("此清單只依規則篩選與排序，排序方法是否有效請看回溯分析的「全市場掃描驗證」，不構成投資建議。")
    return 0


def run_track(cfg, args) -> int:
    from . import tracking
    from .dates import norm_date
    db, st = open_all(cfg)
    if args.since:
        since = norm_date(args.since)
        dates = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? ORDER BY date", (since,))]
        print(f"補建 {len(dates)} 個交易日的追蹤紀錄（{since} 起）…", flush=True)
        step = max(1, len(dates) // 20)
        n = tracking.record(db, st.get(), dates,
                            lambda i, t, d: print(f"  {i}/{t}（{d}）", flush=True) if i % step == 0 or i == t else None)
        print(f"  寫入 {n} 筆", flush=True)
    print(f"回填報酬：{tracking.update_forward(db)} 筆", flush=True)
    s = tracking.refresh(db)
    print(f"模型狀態：{s['status']}。{s['text']}")
    return 0


def run_backtest(cfg, args) -> int:
    import csv
    from datetime import datetime
    from pathlib import Path

    from . import backtest as B
    from .backtest_report import render
    db, st = open_all(cfg)
    dates = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]
    if len(dates) < 120:
        print(f"資料庫只有 {len(dates)} 個交易日，回溯分析至少需要 120 天（含 61 天暖身）。\n"
              f"請先執行：python -m twradar backfill --days 560（約兩年，需要數小時）")
        return 1
    end = norm_date(args.end) if args.end else dates[-1]
    usable = [d for d in dates[61:] if d <= end]
    start = norm_date(args.start) if args.start else usable[max(0, len(usable) - 500)]
    period = [d for d in usable if d >= start]
    split = norm_date(args.split) if args.split else period[len(period) * 2 // 3]
    try:
        variants = B.parse_grid(args.grid) + B.preset_variants(args.compare, st.get())
        conf = B.BTConfig(start=start, end=end, split=split, universe=args.universe, max_adds=args.max_adds, variants=variants,
                          study=not args.no_study, scan=not args.no_scan)
        nv = len(variants) + (1 if conf.scan else 0)
        last = [-1]

        def progress(vi, _nv, ci, nc):
            pct = int((vi + ci / nc) / nv * 100)
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                what = f"{vi + 1}/{len(variants)} 組" if vi < len(variants) else "全市場掃描驗證"
                print(f"  {pct}%（{what}）", flush=True)
        print(f"回溯分析 {start}～{end}，{split} 起為樣本外，{len(variants)} 組規則"
              f"{'＋條件研究' if conf.study else ''}{'＋全市場掃描驗證' if conf.scan else ''}…", flush=True)
        res = B.run(db, st.get(), conf, progress)
    except ValueError as e:
        print(f"錯誤：{e}")
        return 1
    out = Path(args.out) if args.out else home_dir() / "backtest" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    trades = res["results"][0].pop("tradeList", [])
    (out / "report.html").write_text(render({**res, "results": [{**res["results"][0], "tradeList": trades}, *res["results"][1:]]}),
                                     encoding="utf-8")
    (out / "result.json").write_text(B.to_json(res), encoding="utf-8")
    with open(out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代號", "買進日", "出場日", "天數", "進場", "出場原因", "加碼次數", "減碼", "報酬%", "最差浮虧%", "股票組"])
        for t in trades:
            w.writerow([t["code"], t["entry"], t["exit"], t["days"], t["rule"], t["reason"], t["adds"], t["reduced"], t["ret"],
                        t["mae"], "、".join(t["tags"])])
    print(f"完成。報表：{out / 'report.html'}\n（result.json 與 trades.csv 也在同一個資料夾，可傳給維護者分析）")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="twradar", description="台股右側加碼雷達")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    c = sub.add_parser("crawl"); c.add_argument("--date"); c.add_argument("--final", action="store_true")
    b = sub.add_parser("backfill"); b.add_argument("--days", type=int)
    a = sub.add_parser("analyze"); a.add_argument("--date")
    dc = sub.add_parser("doctor"); dc.add_argument("--date")
    pr = sub.add_parser("probe"); pr.add_argument("--date")
    sub.add_parser("info")
    sh = sub.add_parser("show"); sh.add_argument("code"); sh.add_argument("--date")
    sub.add_parser("watchlist")
    im = sub.add_parser("import"); im.add_argument("file")
    sub.add_parser("version")
    hh = sub.add_parser("holders-history", help="集保股權分散表歷史回補（約一年，可中斷後接著跑）")
    hh.add_argument("--codes", default="universe", help="universe（回溯股票池＋觀察清單，預設）、watchlist，或逗號分隔代號")
    hh.add_argument("--weeks", type=int, default=0, help="只抓最近幾週（0＝查詢頁上全部）")
    hh.add_argument("--interval", type=float, default=3.0, help="每次查詢間隔秒數（預設 3；太短可能被封鎖）")
    vs = sub.add_parser("vector-study", help="驗證大盤資金向量（現貨、台指期、匯率的淨方向與分歧度）對未來大盤的區分力")
    vs.add_argument("--window", default="60", help="滾動 Z-score 視窗（逗號分隔，例如 60,120）")
    vs.add_argument("--horizons", default="10,20", help="未來天數（逗號分隔）")
    vs.add_argument("--split", help="樣本外起始日（預設：可用期間的前 2/3 之後）")
    vs.add_argument("--start", help="開始日")
    lh = sub.add_parser("long-history", help="長期歷史：加權指數、0050、加權報酬指數（驗證大盤進出規則用）")
    lh.add_argument("--start", default="20080101", help="開始月份（預設 20080101）")
    lh.add_argument("--end", help="結束日（預設：今天）")
    lh.add_argument("--series", default="taiex,0050,tr", help="taiex、0050、tr（逗號分隔）")
    lh.add_argument("--interval", type=float, default=8.0, help="每個請求間隔秒數（預設 8；太快會被證交所暫時封鎖）")
    ts = sub.add_parser("timing-study", help="長期歷史上比較「抱 0050、大盤空頭時空手」各種規則")
    ts.add_argument("--start", help="開始日（預設：資料最早一天）")
    ts.add_argument("--asset", default="0050,tr", help="0050、tr（逗號分隔）")
    ts.add_argument("--years", action="store_true", help="另外列出每年的報酬")
    mh = sub.add_parser("macro-history", help="大盤環境歷史：外資台指期、新台幣匯率（期交所）與市場廣度")
    mh.add_argument("--start", help="開始日（預設：資料庫第一個交易日）")
    mh.add_argument("--end", help="結束日（預設：今天）")
    mh.add_argument("--no-fetch", action="store_true", help="不抓期交所，只計算市場廣度")
    al = sub.add_parser("alerts", help="注意股、處置股：抓取並列出")
    al.add_argument("--date", help="日期（預設：最新交易日）")
    al.add_argument("--no-fetch", action="store_true", help="只列出資料庫裡的，不抓取")
    sc = sub.add_parser("scan", help="全市場強勢股掃描")
    sc.add_argument("--date", help="日期（預設：最新交易日）")
    sc.add_argument("--method", default="法人", help="法人（預設）、相對強度、相對強度＋不追高、綜合、綜合＋不追高")
    sc.add_argument("--top", type=int, default=20)
    tk = sub.add_parser("track", help="模型追蹤：補建過去的紀錄並更新摘要")
    tk.add_argument("--since", help="從這天（YYYYMMDD）起補建，預設只更新報酬與摘要")
    bt = sub.add_parser("backtest", help="回溯分析")
    bt.add_argument("--start", help="開始日（預設：資料庫最後一天往前約兩年，至少保留 61 天暖身）")
    bt.add_argument("--end", help="結束日（預設：資料庫最後一天）")
    bt.add_argument("--split", help="樣本外起始日（預設：期間的後三分之一）")
    bt.add_argument("--universe", default="auto", help="auto（預設）、watchlist，或 CSV 檔（代號,組別）")
    bt.add_argument("--grid", action="append", default=[], help="參數組合，例：--grid profitTargetPct=8,10,15（可重複）")
    bt.add_argument("--compare", action="append", default=[],
                    help="規則對照組：tdcc（集保籌碼集中）、settled（起漲前籌碼沉澱）、trial（試單）、bullhold／bullstop／bull（依大盤環境調整出場）、v04／v03（前幾版規則）、veto（否決條件逐條拿掉）、inst1d（法人改回看單日）、market（不用大盤逆風），可用逗號合併")
    bt.add_argument("--no-study", action="store_true", help="不做條件研究")
    bt.add_argument("--no-scan", action="store_true", help="不做全市場掃描驗證")
    bt.add_argument("--max-adds", type=int, default=2)
    bt.add_argument("--out", help="輸出資料夾（預設：資料夾/backtest/時間）")
    ad = sub.add_parser("admin"); ad.add_argument("action", choices=["reset-password", "set-password", "rotate-token"])
    ad.add_argument("kind", nargs="?", choices=["read", "admin"])
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd in (None, "serve"):
        setup_logging(cfg)
        from .server import App, serve
        db, st = open_all(cfg)
        print(info_text(cfg, secrets=False), flush=True)
        serve(App(cfg, db, st))
    elif args.cmd == "crawl":
        setup_logging(cfg)
        db, st = open_all(cfg)
        d = norm_date(args.date) if args.date else today()
        print(json.dumps(Crawler(db, cfg, st).crawl_day(d, final=args.final), ensure_ascii=False, indent=1))
    elif args.cmd == "backfill":
        setup_logging(cfg)
        db, st = open_all(cfg)
        Crawler(db, cfg, st).backfill(args.days)
    elif args.cmd == "analyze":
        db, st = open_all(cfg)
        d = norm_date(args.date) if args.date else (db.trading_dates(limit=1) or [today()])[0]
        print(run_analysis(db, d, st))
    elif args.cmd == "doctor":
        sys.exit(doctor(cfg, args.date))
    elif args.cmd == "probe":
        probe(cfg, args.date)
    elif args.cmd == "show":
        from .analysis import evaluate, indicators, rules_of
        db, st = open_all(cfg)
        d = norm_date(args.date) if args.date else (db.trading_dates(limit=1) or [today()])[0]
        rec = indicators(db, args.code.upper(), d)
        w = next((x for x in st.watchlist() if x["code"] == args.code.upper()), {})
        print(json.dumps({"date": d, "code": args.code.upper(), "data": rec,
                          "signal": evaluate(rec, w, rules_of(st), st.get())}, ensure_ascii=False, indent=1))
    elif args.cmd == "info":
        open_all(cfg)  # 確保已完成升級
        print(info_text(cfg))
    elif args.cmd == "watchlist":
        for w in open_all(cfg)[1].watchlist():
            print(f"{w['code']}\t{w['name']}\t{w['market']}\t{','.join(w['labels'])}")
    elif args.cmd == "import":
        _, st = open_all(cfg)
        data = json.load(open(args.file, encoding="utf-8"))
        wl = data.get("watchlist") or []
        items = [stock(code=str(s["code"]).upper(), name=s["name"], market=s.get("market", "TWSE"),
                       group=s.get("group", ""), note=s.get("note", ""), keyLevel=s.get("keyLevel")) for s in wl]

        def merge(cur):
            keep = {w["code"]: w for w in cur["watchlist"]}
            for it in items:
                keep[it["code"]] = {**keep.get(it["code"], {}), **it} if it["code"] in keep else it
            order = [it["code"] for it in items] + [c for c in keep if c not in {i["code"] for i in items}]
            cur["watchlist"] = [keep[c] for c in order]
        st.update(merge)
        print(f"已匯入 {len(items)} 檔")
    elif args.cmd == "backtest":
        sys.exit(run_backtest(cfg, args))
    elif args.cmd == "track":
        sys.exit(run_track(cfg, args))
    elif args.cmd == "holders-history":
        sys.exit(run_holders_history(cfg, args))
    elif args.cmd == "vector-study":
        sys.exit(run_vector_study(cfg, args))
    elif args.cmd == "long-history":
        sys.exit(run_long_history(cfg, args))
    elif args.cmd == "timing-study":
        sys.exit(run_timing_study(cfg, args))
    elif args.cmd == "macro-history":
        sys.exit(run_macro_history(cfg, args))
    elif args.cmd == "alerts":
        sys.exit(run_alerts(cfg, args))
    elif args.cmd == "scan":
        sys.exit(run_scan(cfg, args))
    elif args.cmd == "admin":
        from .auth import AuthError, TokenPasswordAuthenticator
        open_all(cfg)
        auth = TokenPasswordAuthenticator(cfg)
        if args.action == "reset-password":
            print(f"新的管理密碼：{auth.reset_password()}\n（所有已登入的裝置需要重新登入；登入後可在「設定 → 權限」改成自己的密碼）")
        elif args.action == "set-password":
            import getpass
            pw = getpass.getpass("新密碼（至少 8 字元）：")
            if pw != getpass.getpass("再輸入一次："):
                sys.exit("兩次輸入不一致")
            try:
                auth.change_password_local(pw)
            except AuthError as e:
                sys.exit(str(e))
            print("已設定。所有已登入的裝置需要重新登入。")
        elif args.action == "rotate-token":
            if not args.kind:
                sys.exit("請指定 read 或 admin，例如：twradar admin rotate-token read")
            print(f"新的{'唯讀' if args.kind == 'read' else '管理'} token：{auth.rotate_token(args.kind)}")
    elif args.cmd == "version":
        print(__version__)
