"""指令列：python -m twradar <指令>

  serve                 啟動網頁、API 與每日排程（安裝後由系統服務自動執行）
  crawl [--date D] [--final]   立即抓取某一天
  backfill [--days N]   補抓歷史資料
  analyze [--date D]    重新分析
  doctor [--date D]     測試各資料來源是否可以連線、格式是否正確
  probe [--date D]      列出櫃買中心各網址實際回傳內容（給維護者除錯用）
  info                  顯示網址與 token
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
from .providers import build
from .providers.common import dates_in, tables

SEED = [  # 首次啟動的預設觀察清單
    ("2330", "台積電", "TWSE", "科技／半導體權值", None, "已建 100 股底倉；看月線回踩是否守住"),
    ("2454", "聯發科", "TWSE", "科技／半導體權值", 4000, "已建 100 股底倉；看是否帶量站穩 4,000 月線反壓區"),
    ("2303", "聯電", "TWSE", "科技／半導體權值", None, "成熟製程晶圓代工"),
    ("6488", "環球晶", "TPEx", "科技／半導體權值", None, "矽晶圓強勢反彈代表"),
    ("6669", "緯穎", "TWSE", "AI 硬體／伺服器", None, "AI 伺服器"),
    ("3081", "聯亞", "TPEx", "AI 硬體／矽光子", None, "矽光子"),
    ("3163", "波若威", "TPEx", "AI 硬體／網通", None, "網通光通訊"),
    ("3017", "奇鋐", "TWSE", "AI 硬體／散熱", None, "看外資是否由賣轉買、連續性"),
    ("8358", "金居", "TPEx", "AI 硬體／CCL", None, "CCL 銅箔基板；看主力分點是否持續鎖碼"),
    ("2383", "台光電", "TWSE", "AI 硬體／CCL", None, "高階 CCL"),
    ("3443", "創意", "TWSE", "AI 硬體／ASIC", None, "ASIC／矽智財"),
    ("2881", "富邦金", "TWSE", "金融權值防禦", None, ""),
    ("2885", "元大金", "TWSE", "金融權值防禦", None, ""),
]


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
    db = DB(cfg.db_path)
    if not db.get_setting("seeded"):
        for i, (code, name, mkt, grp, kl, note) in enumerate(SEED):
            db.upsert_stock({"code": code, "name": name, "market": mkt, "grp": grp, "note": note,
                             "key_level": kl, "sort_order": i + 1})
        db.set_setting("seeded", True)
    return db


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def info_text(cfg) -> str:
    ip = lan_ip()
    return (f"twradar {__version__}\n"
            f"資料夾：{home_dir()}\n"
            f"本機網址：http://localhost:{cfg.port}/#t={cfg.token}\n"
            f"區網網址：http://{ip}:{cfg.port}/#t={cfg.token}\n"
            f"API token：{cfg.token}\n"
            f"API 範例：http://{ip}:{cfg.port}/api/v1/latest?token={cfg.token}\n")


def doctor(cfg, date: str | None = None, transport=None) -> int:
    f = transport or Fetcher(max(3.0, cfg.request_interval), cfg.user_agent)
    d = norm_date(date) if date else prev_weekday(today())
    primary, fallback, opts = cfg.source_plan()
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
    _, _, opts = cfg.source_plan()
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
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd in (None, "serve"):
        setup_logging(cfg)
        from .server import App, serve
        db = open_db(cfg)
        print(info_text(cfg), flush=True)
        serve(App(cfg, db))
    elif args.cmd == "crawl":
        setup_logging(cfg)
        db = open_db(cfg)
        d = norm_date(args.date) if args.date else today()
        print(json.dumps(Crawler(db, cfg).crawl_day(d, final=args.final), ensure_ascii=False, indent=1))
    elif args.cmd == "backfill":
        setup_logging(cfg)
        Crawler(open_db(cfg), cfg).backfill(args.days)
    elif args.cmd == "analyze":
        db = open_db(cfg)
        d = norm_date(args.date) if args.date else (db.trading_dates(limit=1) or [today()])[0]
        print(run_analysis(db, d))
    elif args.cmd == "doctor":
        sys.exit(doctor(cfg, args.date))
    elif args.cmd == "probe":
        probe(cfg, args.date)
    elif args.cmd == "show":
        from .analysis import evaluate, indicators, rules_of
        db = open_db(cfg)
        d = norm_date(args.date) if args.date else (db.trading_dates(limit=1) or [today()])[0]
        rec = indicators(db, args.code.upper(), d)
        w = next((x for x in db.watchlist() if x["code"] == args.code.upper()), {})
        print(json.dumps({"date": d, "code": args.code.upper(), "data": rec,
                          "signal": evaluate(rec, w, rules_of(db))}, ensure_ascii=False, indent=1))
    elif args.cmd == "info":
        print(info_text(cfg))
    elif args.cmd == "watchlist":
        for w in open_db(cfg).watchlist():
            print(f"{w['code']}\t{w['name']}\t{w['market']}\t{w['grp']}")
    elif args.cmd == "import":
        db = open_db(cfg)
        data = json.load(open(args.file, encoding="utf-8"))
        wl = data.get("watchlist") or []
        for i, s in enumerate(wl):
            db.upsert_stock({"code": str(s["code"]), "name": s["name"], "market": s.get("market", "TWSE"),
                             "grp": s.get("group", ""), "note": s.get("note", ""), "key_level": s.get("keyLevel"),
                             "sort_order": s.get("order", i + 1)})
        print(f"已匯入 {len(wl)} 檔")
    elif args.cmd == "version":
        print(__version__)
