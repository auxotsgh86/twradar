"""Web API 與網頁 UI（標準函式庫 http.server）。

所有 /api/v1 端點（health 除外）都要 token：
  Header `Authorization: Bearer <token>`、`X-Token: <token>`，或網址參數 `?token=<token>`。
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import mimetypes
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .dates import is_weekend, norm_date
from .analysis import DEFAULT_RULES, LIGHT_LABEL, evaluate, indicators_many, rules_of, run_analysis
from .config import Config, now_tw
from .db import DB
from .jobs import STATUS_TEXT, Crawler, Scheduler, Worker, prev_weekday, today

log = logging.getLogger("twradar.server")
WEB = Path(__file__).parent / "web"

CSV_COLS = ["code", "name", "market", "light", "lightLabel", "close", "chgPct", "vol", "vol5", "ma20", "ma60",
            "prevHigh", "foreign", "trust", "dealer", "foreignStreak", "trustStreak", "foreign5d", "trust5d",
            "marginChg", "shortChg", "volRatio", "maDistPct", "needs", "warnings", "missing", "source"]


def iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    d = norm_date(s)
    return d if d and re.fullmatch(r"\d{8}", d) else None


class App:
    def __init__(self, cfg: Config, db: DB, start_background: bool = True, crawler: Crawler | None = None):
        self.cfg = cfg
        self.db = db
        self.crawler = crawler or Crawler(db, cfg)
        self.worker = Worker(self.crawler)
        self.scheduler = Scheduler(self.crawler, self.worker) if start_background else None

    # ---------- 組資料 ----------
    def market_of(self, d: str) -> dict:
        m = self.db.one("SELECT * FROM market WHERE date=?", (d,))
        if not m:
            return {}
        m = dict(m)
        return {"taiex": m["taiex"], "taiexChg": m["taiex_chg"], "taiexChgPct": m["taiex_chg_pct"],
                "foreignAmt": None if m["foreign_amt"] is None else round(m["foreign_amt"] / 1e8, 2),
                "trustAmt": None if m["trust_amt"] is None else round(m["trust_amt"] / 1e8, 2),
                "dealerAmt": None if m["dealer_amt"] is None else round(m["dealer_amt"] / 1e8, 2),
                "unit": "億元", "source": m["src"]}

    def day_status(self, d: str) -> dict:
        run = self.db.get_run(d)
        an = self.db.one("SELECT * FROM analysis WHERE date=?", (d,))
        if run:
            status = run["status"]
        else:
            status = "holiday" if is_weekend(d) else "not_started"
        a_status = an["status"] if an else "not_started"
        if status in ("running",):
            a_status = "pending" if not an else a_status
        ready = status == "complete" and a_status == "complete"
        if ready:
            text = "抓取與分析皆已完成"
        elif status == "holiday":
            text = "休市"
        else:
            text = STATUS_TEXT.get(status, status) + ("；分析為暫定結果" if a_status == "provisional" else "")
        return {
            "date": iso(d), "status": status, "ready": ready, "statusText": text,
            "crawl": {"status": status, "attempts": run["attempts"] if run else 0,
                      "startedAt": run["started_at"] if run else None, "finishedAt": run["finished_at"] if run else None,
                      "message": run["message"] if run else "",
                      "steps": {s: {"label": self.crawler.step_labels[s], **(run["steps"].get(s, {"status": "pending"}))}
                                for s in self.crawler.step_ids} if run and run["steps"] else {}},
            "analysis": {"status": a_status, "analyzedAt": an["analyzed_at"] if an else None,
                         "summary": json.loads(an["summary"]) if an and an["summary"] else None},
        }

    def day_stocks(self, d: str) -> list[dict]:
        sigs = {r["code"]: json.loads(r["payload"]) for r in self.db.q("SELECT code, payload FROM signals WHERE date=?", (d,))}
        R = rules_of(self.db)
        out = []
        watch = self.db.watchlist()
        late = indicators_many(self.db, [w["code"] for w in watch if w["code"] not in sigs], d)
        for w in watch:
            p = sigs.get(w["code"])
            if not p:  # 分析之後才加入的個股：即時計算
                rec = late[w["code"]]
                p = {"data": rec, "signal": evaluate(rec, w, R)}
            out.append({"code": w["code"], "name": w["name"], "market": w["market"], "group": w["grp"],
                        "note": w["note"], "keyLevel": w["key_level"], "data": p["data"], "signal": p["signal"]})
        return out

    def envelope(self, d: str) -> dict:
        st = self.day_status(d)
        stocks = self.day_stocks(d) if st["status"] not in ("holiday", "not_started") else []
        summary = {"go": 0, "wait": 0, "stop": 0, "na": 0}
        for s in stocks:
            summary[s["signal"]["light"]] += 1
        return {"apiVersion": 1, **st, "generatedAt": now_tw().isoformat(timespec="seconds"),
                "rules": rules_of(self.db), "market": self.market_of(d), "summary": summary, "stocks": stocks}

    def latest_date(self, ready_only: bool = False) -> str | None:
        rows = self.db.q("SELECT date, status FROM runs WHERE status!='holiday' ORDER BY date DESC LIMIT 30")
        for r in rows:
            if not ready_only:
                return r["date"]
            if self.day_status(r["date"])["ready"]:
                return r["date"]
        return None

    def radar(self, d: str) -> dict:
        """與「右側加碼雷達」網頁工具相容的匯入格式。"""
        env = self.envelope(d)
        m = env["market"]
        return {"schema": "right-side-radar/1", "date": d,
                "watchlist": [{"code": s["code"], "name": s["name"], "market": s["market"], "group": s["group"] or "",
                               "note": s["note"] or "", "keyLevel": s["keyLevel"], "order": i + 1}
                              for i, s in enumerate(env["stocks"])],
                "days": [{"date": d, "market": {k: m.get(k) for k in ("taiex", "taiexChgPct", "foreignAmt") if m.get(k) is not None},
                          "stocks": {s["code"]: s["data"] for s in env["stocks"] if s["data"]}}]}

    def history(self, code: str, days: int) -> list[dict]:
        q = self.db.q("""SELECT q.date, q.open, q.high, q.low, q.close, q.change, q.volume,
              i.foreign_net, i.trust_net, i.dealer_net, m.margin_bal, m.margin_prev, m.short_bal, m.short_prev
              FROM quotes q LEFT JOIN inst i ON i.code=q.code AND i.date=q.date
              LEFT JOIN margin m ON m.code=q.code AND m.date=q.date
              WHERE q.code=? ORDER BY q.date DESC LIMIT ?""", (code, days + 59))
        rows = [dict(r) for r in q][::-1]
        out = []
        closes = []
        for r in rows:
            closes.append(r["close"])
            c20 = [c for c in closes[-20:] if c is not None]
            c60 = [c for c in closes[-60:] if c is not None]
            lot = lambda v: None if v is None else int(round(v / 1000))
            out.append({"date": iso(r["date"]), "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                        "change": r["change"], "volume": None if r["volume"] is None else round(r["volume"] / 1000, 1),
                        "foreign": lot(r["foreign_net"]), "trust": lot(r["trust_net"]), "dealer": lot(r["dealer_net"]),
                        "marginBal": r["margin_bal"],
                        "marginChg": None if r["margin_bal"] is None or r["margin_prev"] is None else r["margin_bal"] - r["margin_prev"],
                        "shortBal": r["short_bal"],
                        "ma20": round(sum(c20) / 20, 2) if len(c20) == 20 else None,
                        "ma60": round(sum(c60) / 60, 2) if len(c60) == 60 else None})
        return out[-days:]

    def system(self) -> dict:
        runs = [dict(r) for r in self.db.q("SELECT date,status,attempts,finished_at,message FROM runs ORDER BY date DESC LIMIT 15")]
        n_days = self.db.one("SELECT COUNT(DISTINCT date) AS n FROM quotes")["n"]
        return {"version": __version__, "now": now_tw().isoformat(timespec="seconds"),
                "worker": self.worker.state(), "backfill": self.db.get_setting("backfill"),
                "nextRuns": self.scheduler.next_runs() if self.scheduler else [],
                "tradingDaysStored": n_days, "watchlistSize": len(self.db.watchlist()),
                "maxWatchlist": self.cfg.max_watchlist, "finmindFallback": self.cfg.use_finmind_fallback,
                "recentRuns": runs}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "twradar/" + __version__

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # ---------- 回應 ----------
        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, X-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, obj, code: int = 200):
            self.send(code, json.dumps(obj, ensure_ascii=False, indent=1).encode(), "application/json; charset=utf-8")

        def err(self, code: int, msg: str):
            self.json({"error": msg, "status": code}, code)

        def csv_out(self, rows: list[dict], cols: list[str], filename: str):
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("；".join(v) if isinstance(v, list) else v) for k, v in r.items()})
            self.send(200, ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{filename}"'})

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def authed(self, qs) -> bool:
            tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
                or self.headers.get("X-Token") or (qs.get("token") or [""])[0]
            return bool(tok) and hmac.compare_digest(tok, app.cfg.token)

        # ---------- 路由 ----------
        def do_OPTIONS(self):
            self.send(204, b"", "text/plain")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method: str):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = u.path.rstrip("/") or "/"
            try:
                if not p.startswith("/api/"):
                    return self.static(p)
                if p == "/api/v1/health":
                    return self.json({"ok": True, "version": __version__, "now": now_tw().isoformat(timespec="seconds")})
                if not self.authed(qs):
                    return self.err(401, "需要 token（Authorization: Bearer <token> 或 ?token=）")
                return self.api(method, p, qs)
            except Exception as e:
                log.exception("request failed")
                return self.err(500, f"伺服器錯誤：{e!r}")

        def static(self, p: str):
            name = "index.html" if p == "/" else p.lstrip("/")
            f = (WEB / name).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self.err(404, "找不到頁面")
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send(200, f.read_bytes(), ctype)

        def api(self, method: str, p: str, qs):
            fmt = (qs.get("format") or ["json"])[0]
            parts = p.split("/")[3:]  # 去掉 '', 'api', 'v1'
            head = parts[0] if parts else ""

            if head == "status" and method == "GET":
                return self.json(app.system())

            if head == "dates" and method == "GET":
                rows = app.db.q("SELECT r.date, r.status, a.status AS astatus, a.summary FROM runs r "
                                "LEFT JOIN analysis a ON a.date=r.date ORDER BY r.date DESC LIMIT 400")
                return self.json([{"date": iso(r["date"]), "status": r["status"],
                                   "analysisStatus": r["astatus"] or "not_started",
                                   "ready": r["status"] == "complete" and r["astatus"] == "complete",
                                   "summary": json.loads(r["summary"]) if r["summary"] else None} for r in rows])

            if head in ("latest", "days") and method == "GET":
                if head == "latest":
                    d = app.latest_date(ready_only=(qs.get("ready") or ["0"])[0] in ("1", "true"))
                    if not d:
                        return self.json({"apiVersion": 1, "status": "not_started", "ready": False,
                                          "statusText": "還沒有任何抓取紀錄", "stocks": []})
                else:
                    d = parse_date(parts[1] if len(parts) > 1 else None)
                    if not d:
                        return self.err(400, "日期格式錯誤，請用 YYYY-MM-DD 或 YYYYMMDD")
                if fmt == "csv":
                    env = app.envelope(d)
                    rows = [{"code": s["code"], "name": s["name"], "market": s["market"], **s["data"],
                             **{k: s["signal"][k] for k in ("light", "lightLabel", "needs", "warnings", "missing",
                                                            "volRatio", "maDistPct")}} for s in env["stocks"]]
                    return self.csv_out(rows, CSV_COLS, f"twradar-{d}.csv")
                if fmt == "radar":
                    return self.json(app.radar(d))
                if fmt == "status":
                    return self.json(app.day_status(d))
                return self.json(app.envelope(d))

            if head == "stocks" and len(parts) >= 3 and parts[2] == "history" and method == "GET":
                code = parts[1].upper()
                days = max(5, min(400, int((qs.get("days") or ["120"])[0])))
                rows = app.history(code, days)
                if fmt == "csv":
                    return self.csv_out(rows, list(rows[0].keys()) if rows else ["date"], f"twradar-{code}-history.csv")
                w = next((x for x in app.db.watchlist() if x["code"] == code), None)
                return self.json({"code": code, "name": w["name"] if w else None, "days": len(rows), "rows": rows})

            if head == "watchlist":
                if method == "GET":
                    return self.json(app.db.watchlist())
                if method == "POST":
                    b = self.body()
                    code = str(b.get("code", "")).strip().upper()
                    if not re.fullmatch(r"[0-9A-Z]{4,6}", code) or not b.get("name"):
                        return self.err(400, "需要 code（4–6 碼）與 name")
                    wl = app.db.watchlist()
                    if code not in {x["code"] for x in wl} and len(wl) >= app.cfg.max_watchlist:
                        return self.err(400, f"觀察清單上限 {app.cfg.max_watchlist} 檔")
                    market = "TPEx" if str(b.get("market", "")).upper() in ("TPEX", "OTC", "上櫃") else "TWSE"
                    stock = {"code": code, "name": str(b["name"]).strip(), "market": market,
                             "grp": str(b.get("group", b.get("grp", ""))), "note": str(b.get("note", "")),
                             "key_level": b.get("keyLevel", b.get("key_level")),
                             "sort_order": int(b.get("order", b.get("sort_order", len(wl) + 1)) or 999)}
                    app.db.upsert_stock(stock)
                    app.worker.submit(f"補 {code} 歷史", lambda: app.crawler.ensure_history(stock))
                    return self.json({"ok": True, "stock": stock})
                if method == "DELETE" and len(parts) > 1:
                    app.db.delete_stock(parts[1].upper())
                    return self.json({"ok": True})

            if head == "rules":
                if method == "GET":
                    return self.json({"rules": rules_of(app.db), "defaults": DEFAULT_RULES})
                if method == "PUT":
                    b = self.body()
                    new = {k: float(b[k]) for k in DEFAULT_RULES if k in b and isinstance(b[k], (int, float))}
                    app.db.set_setting("rules", {**rules_of(app.db), **new})
                    d = app.latest_date()
                    if d:
                        run_analysis(app.db, d)
                    return self.json({"ok": True, "rules": rules_of(app.db)})

            if head == "crawl" and method == "POST":
                b = self.body()
                d = parse_date(b.get("date")) or (today() if now_tw().hour >= 15 else prev_weekday(today()))
                final = bool(b.get("final"))
                queued = app.worker.submit(f"抓取 {d}{'（最後一次）' if final else ''}（手動）",
                                           lambda: app.crawler.crawl_day(d, final=final))
                return self.json({"ok": True, "queued": queued, "date": iso(d)})

            if head == "analyze" and method == "POST":
                d = parse_date(self.body().get("date")) or app.latest_date()
                if not d:
                    return self.err(400, "沒有可分析的日期")
                return self.json({"ok": True, "date": iso(d), "summary": run_analysis(app.db, d)})

            if head == "backfill" and method == "POST":
                days = int(self.body().get("days") or app.cfg.backfill_days)
                return self.json({"ok": True, "queued": app.worker.submit("補抓歷史資料", lambda: app.crawler.backfill(days))})

            if head == "events" and method == "GET":
                lim = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
                return self.json([dict(r) for r in app.db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (lim,))])

            if head == "export" and len(parts) > 1 and parts[1] == "sqlite" and method == "GET":
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "twradar.sqlite3"
                    app.db.backup_to(str(dest))
                    data = dest.read_bytes()
                stamp = now_tw().strftime("%Y%m%d-%H%M")
                return self.send(200, data, "application/vnd.sqlite3",
                                 {"Content-Disposition": f'attachment; filename="twradar-{stamp}.sqlite3"'})

            return self.err(404, "找不到這個 API")

    return H


def serve(app: App) -> None:
    httpd = ThreadingHTTPServer((app.cfg.host, app.cfg.port), make_handler(app))
    httpd.daemon_threads = True
    log.info("twradar %s 已啟動：http://%s:%d", __version__, app.cfg.host, app.cfg.port)
    httpd.serve_forever()
