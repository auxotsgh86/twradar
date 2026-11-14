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
        variants = B.parse_grid(args.grid)
        conf = B.BTConfig(start=start, end=end, split=split, universe=args.universe, max_adds=args.max_adds, variants=variants)
        last = [-1]

        def progress(vi, nv, ci, nc):
            pct = int((vi + ci / nc) / nv * 100)
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                print(f"  {pct}%（{vi + 1}/{nv} 組）", flush=True)
        print(f"回溯分析 {start}～{end}，{split} 起為樣本外，{len(variants)} 組參數…", flush=True)
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
    tk = sub.add_parser("track", help="模型追蹤：補建過去的紀錄並更新摘要")
    tk.add_argument("--since", help="從這天（YYYYMMDD）起補建，預設只更新報酬與摘要")
    bt = sub.add_parser("backtest", help="回溯分析")
    bt.add_argument("--start", help="開始日（預設：資料庫最後一天往前約兩年，至少保留 61 天暖身）")
    bt.add_argument("--end", help="結束日（預設：資料庫最後一天）")
    bt.add_argument("--split", help="樣本外起始日（預設：期間的後三分之一）")
    bt.add_argument("--universe", default="auto", help="auto（預設）、watchlist，或 CSV 檔（代號,組別）")
    bt.add_argument("--grid", action="append", default=[], help="參數組合，例：--grid profitTargetPct=8,10,15（可重複）")
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
