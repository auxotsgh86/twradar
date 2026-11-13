"""Web API 與網頁 UI（標準函式庫 http.server）。

權限（見 auth.py）：
- 唯讀：唯讀 token（`Authorization: Bearer`、`X-Token` 或 `?token=`），可讀取所有資料。
- 管理者：管理 token（只接受標頭）或網頁密碼登入（cookie），可修改設定、觸發抓取、匯出資料庫。
不需登入：/api/v1/health、/api/v1/auth/login、/api/v1/auth/me、/api/v1/auth/logout。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import re
import tempfile
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .dates import is_weekend, norm_date
from .analysis import DEFAULT_RULES, LIGHT_LABEL, evaluate, indicators_many, rules_of, run_analysis
from .auth import COOKIE, SESSION_DAYS, AuthError, Authenticator, Credentials, TokenPasswordAuthenticator
from .settings import SettingsError, SettingsStore, mask, stock as new_stock
from .config import Config, now_tw
from .db import DB
from .jobs import STATUS_TEXT, Crawler, Scheduler, Worker, prev_weekday, today

log = logging.getLogger("twradar.server")
ADMIN_ONLY = ("settings", "export")  # 這些路徑連讀取都需要管理者
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
    def __init__(self, cfg: Config, db: DB, settings: SettingsStore, start_background: bool = True,
                 crawler: Crawler | None = None, authenticator: Authenticator | None = None):
        self.cfg = cfg
        self.db = db
        self.settings = settings
        self.auth = authenticator or TokenPasswordAuthenticator(cfg)
        self.crawler = crawler or Crawler(db, cfg, settings)
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
        S = self.settings.get()
        R = rules_of(self.settings)
        out = []
        watch = S["watchlist"]
        late = indicators_many(self.db, [w["code"] for w in watch if w["code"] not in sigs], d, R, {w["code"]: w for w in watch})
        for w in watch:
            p = sigs.get(w["code"])
            if not p:  # 分析之後才加入的個股：即時計算
                rec = late[w["code"]]
                p = {"data": rec, "signal": evaluate(rec, w, R, S)}
            out.append({"code": w["code"], "name": w["name"], "market": w["market"], "group": w["group"],
                        "note": w["note"], "keyLevel": w["keyLevel"], "holding": w["holding"], "cost": w["cost"],
                        "data": p["data"], "signal": p["signal"]})
        return out

    def envelope(self, d: str) -> dict:
        st = self.day_status(d)
        stocks = self.day_stocks(d) if st["status"] not in ("holiday", "not_started") else []
        summary = {"go": 0, "wait": 0, "stop": 0, "na": 0}
        for s in stocks:
            summary[s["signal"]["light"]] += 1
        return {"apiVersion": 1, **st, "generatedAt": now_tw().isoformat(timespec="seconds"),
                "rules": rules_of(self.settings), "market": self.market_of(d), "summary": summary, "stocks": stocks}

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

    def reanalyze_latest(self) -> None:
        d = self.latest_date()
        if d:
            run_analysis(self.db, d, self.settings)

    def system(self) -> dict:
        runs = [dict(r) for r in self.db.q("SELECT date,status,attempts,finished_at,message FROM runs ORDER BY date DESC LIMIT 15")]
        n_days = self.db.one("SELECT COUNT(DISTINCT date) AS n FROM quotes")["n"]
        return {"version": __version__, "now": now_tw().isoformat(timespec="seconds"),
                "worker": self.worker.state(), "backfill": self.db.get_setting("backfill"),
                "nextRuns": self.scheduler.next_runs() if self.scheduler else [],
                "tradingDaysStored": n_days, "watchlistSize": len(self.settings.watchlist()),
                "maxWatchlist": self.cfg.max_watchlist, "finmindFallback": bool(self.settings.source_plan()[1]),
                "sources": {"primary": self.crawler.step_ids, "fallback": [p.id for p in self.crawler.fallback]},
                "settingsError": self.settings.last_error, "holdersDate": self.db.holders_latest(),
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

        def json(self, obj, code: int = 200, extra: dict | None = None):
            self.send(code, json.dumps(obj, ensure_ascii=False, indent=1).encode(), "application/json; charset=utf-8", extra)

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

        def credentials(self, qs) -> Credentials:
            cookie = ""
            try:
                jar = SimpleCookie(self.headers.get("Cookie") or "")
                if COOKIE in jar:
                    cookie = jar[COOKIE].value
            except CookieError:
                pass
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() or self.headers.get("X-Token") or ""
            return Credentials(bearer=bearer.strip(), query_token=(qs.get("token") or [""])[0], cookie=cookie,
                               client=self.client_address[0] if self.client_address else "")

        def session_cookie(self, value: str, max_age: int) -> dict:
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            return {"Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}"}

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
                cred = self.credentials(qs)
                if p.startswith("/api/v1/auth/"):
                    return self.auth_api(method, p.split("/")[4] if len(p.split("/")) > 4 else "", cred)
                who = app.auth.authenticate(cred)
                if not who:
                    return self.err(401, "需要 token（Authorization: Bearer <token> 或 ?token=）或管理者登入")
                parts = p.split("/")[3:]
                if (method != "GET" or (parts and parts[0] in ADMIN_ONLY)) and not who.is_admin:
                    return self.err(403, "這個動作需要管理者權限（請用管理密碼登入，或使用管理 token）")
                self.who = who
                return self.api(method, p, qs)
            except AuthError as e:
                return self.err(e.status, str(e))
            except SettingsError as e:
                return self.json({"error": "設定不合法：" + str(e), "status": 400, "errors": e.errors}, 400)
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

        def auth_api(self, method: str, action: str, cred: Credentials):
            if action == "login" and method == "POST":
                cookie = app.auth.login(str(self.body().get("password") or ""), cred.client)
                app.db.log("info", f"管理者登入（{cred.client}）")
                return self.json({"ok": True, "role": "admin"}, extra=self.session_cookie(cookie, SESSION_DAYS * 86400))
            if action == "logout" and method == "POST":
                return self.json({"ok": True}, extra=self.session_cookie("", 0))
            who = app.auth.authenticate(cred)
            if action == "me" and method == "GET":
                return self.json({"role": who.role if who else None, "via": who.via if who else None,
                                  "initialPassword": bool(who and who.is_admin and app.cfg.admin_initial_password)})
            if not who or not who.is_admin:
                return self.err(401 if not who else 403, "需要管理者權限")
            if action == "tokens" and method == "GET":
                return self.json(app.auth.tokens())
            if action == "rotate" and method == "POST":
                kind = str(self.body().get("kind") or "")
                tok = app.auth.rotate_token(kind)
                app.db.log("warn", f"{'唯讀' if kind == 'read' else '管理'} token 已重設")
                return self.json({"ok": True, "kind": kind, "token": tok})
            if action == "password" and method == "POST":
                b = self.body()
                cookie = app.auth.change_password(str(b.get("current") or ""), b.get("new"))
                app.db.log("warn", "管理密碼已變更，其他裝置的登入已失效")
                return self.json({"ok": True}, extra=self.session_cookie(cookie, SESSION_DAYS * 86400))
            return self.err(404, "找不到這個 API")

        def settings_api(self, method: str, parts: list[str], qs):
            st = app.settings
            sub = parts[1] if len(parts) > 1 else ""
            if not sub and method == "GET":
                return self.json({"settings": mask(st.get()), "rev": st.rev, "error": st.last_error})
            if not sub and method == "PUT":
                b = self.body()
                saved = st.save(b.get("settings", b))
                app.reanalyze_latest()
                return self.json({"ok": True, "settings": mask(saved), "rev": st.rev})
            if sub == "history" and method == "GET":
                return self.json(st.history())
            if sub == "restore" and method == "POST":
                saved = st.restore(str(self.body().get("id") or ""))
                app.reanalyze_latest()
                return self.json({"ok": True, "settings": mask(saved), "rev": st.rev})
            if sub == "reset" and method == "POST":
                saved = st.reset(str(self.body().get("section") or ""))
                app.reanalyze_latest()
                return self.json({"ok": True, "settings": mask(saved), "rev": st.rev})
            if sub == "export" and method == "GET":
                stamp = now_tw().strftime("%Y%m%d-%H%M")
                body = json.dumps(st.get(), ensure_ascii=False, indent=2).encode()
                return self.send(200, body, "application/json; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="twradar-settings-{stamp}.json"'})
            return self.err(404, "找不到這個 API")

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
                w = next((x for x in app.settings.watchlist() if x["code"] == code), None)
                return self.json({"code": code, "name": w["name"] if w else None, "days": len(rows), "rows": rows})

            if head == "settings":
                return self.settings_api(method, parts, qs)

            if head == "watchlist":
                if method == "GET":
                    return self.json(app.settings.watchlist())
                if method == "POST":
                    b = self.body()
                    market = "TPEx" if str(b.get("market", "")).upper() in ("TPEX", "OTC", "上櫃") else "TWSE"
                    item = new_stock(**{**b, "code": str(b.get("code", "")).strip().upper(),
                                        "name": str(b.get("name") or "").strip(), "market": market})

                    def upsert(s):
                        wl = s["watchlist"]
                        i = next((k for k, w in enumerate(wl) if w["code"] == item["code"]), None)
                        if i is None:
                            pos = b.get("order")
                            wl.insert(int(pos) - 1 if isinstance(pos, int) and pos > 0 else len(wl), item)
                        else:  # 只更新有送來的欄位（舊版網頁不會送 holding、cost 等）
                            given = {k: v for k, v in item.items() if k in b or k in ("code", "name", "market")}
                            if "keyLevel" not in b and "key_level" in b:
                                given["keyLevel"] = b["key_level"]
                            wl[i] = {**wl[i], **given}
                    app.settings.update(upsert)
                    app.worker.submit(f"補 {item['code']} 歷史", lambda: app.crawler.ensure_history(item))
                    return self.json({"ok": True, "stock": item})
                if method == "DELETE" and len(parts) > 1:
                    code = parts[1].upper()
                    app.settings.update(lambda s: s.__setitem__("watchlist", [w for w in s["watchlist"] if w["code"] != code]))
                    return self.json({"ok": True})

            if head == "rules" and len(parts) > 1 and parts[1] == "catalog" and method == "GET":
                from .rules import catalog
                return self.json(catalog(app.settings.get()))

            if head == "rules":
                if method == "GET":
                    return self.json({"rules": rules_of(app.settings), "defaults": DEFAULT_RULES,
                                      "params": app.settings.get()["params"]})
                if method == "PUT":
                    b = self.body()

                    def setv(s):
                        for prm in s["params"]:
                            v = b.get(prm["id"])
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                prm["value"] = v
                    app.settings.update(setv)
                    app.reanalyze_latest()
                    return self.json({"ok": True, "rules": rules_of(app.settings)})

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
                return self.json({"ok": True, "date": iso(d), "summary": run_analysis(app.db, d, app.settings)})

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
