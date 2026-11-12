"""抓取流程、補歷史、背景工作佇列與每日排程。"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import sources as S
from .analysis import run_analysis
from .config import TW, Config, now_tw
from .db import DB
from .net import FetchError, Fetcher

log = logging.getLogger("twradar.jobs")

STEPS = ["twse_quotes", "twse_inst", "twse_margin", "twse_flows", "tpex_quotes", "tpex_inst", "tpex_margin"]
STEP_LABEL = {"twse_quotes": "上市收盤行情", "twse_inst": "上市三大法人", "twse_margin": "上市融資融券",
              "twse_flows": "三大法人金額", "tpex_quotes": "上櫃收盤行情", "tpex_inst": "上櫃三大法人",
              "tpex_margin": "上櫃融資融券"}
DONE = ("ok", "fallback")
STATUS_TEXT = {
    "not_started": "尚未開始抓取", "running": "抓取中", "waiting": "部分資料交易所尚未公布，稍後自動重抓",
    "partial": "已完成抓取，但有資料缺漏", "complete": "抓取完成", "failed": "抓取失敗", "holiday": "休市",
}


def _now() -> str:
    return now_tw().isoformat(timespec="seconds")


def today() -> str:
    return now_tw().strftime("%Y%m%d")


def prev_weekday(d: str) -> str:
    x = datetime.strptime(d, "%Y%m%d") - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x.strftime("%Y%m%d")


class Crawler:
    def __init__(self, db: DB, cfg: Config):
        self.db = db
        self.cfg = cfg
        self.f = Fetcher(cfg.request_interval, cfg.user_agent)

    # ---------- 單一交易日 ----------
    def crawl_day(self, d: str, final: bool = False, analyze: bool = True, fallback: bool = True) -> dict:
        if S.is_weekend(d):
            run = {"date": d, "status": "holiday", "steps": {}, "attempts": 0, "started_at": _now(),
                   "finished_at": _now(), "message": "週末休市"}
            self.db.save_run(run)
            return run
        run = self.db.get_run(d) or {"date": d, "status": "not_started", "steps": {}, "attempts": 0,
                                     "started_at": None, "finished_at": None, "message": ""}
        if run["status"] == "holiday":
            return run
        run.update(status="running", attempts=run["attempts"] + 1, started_at=_now(), finished_at=None)
        for s in STEPS:
            run["steps"].setdefault(s, {"status": "pending"})
        self.db.save_run(run)
        self.db.log("info", f"開始抓取 {d}（第 {run['attempts']} 次{'，最後一次' if final else ''}）")

        handlers = {
            "twse_quotes": self._twse_quotes, "twse_inst": lambda d: self.db.put_inst(S.twse_inst(self.f, d)),
            "twse_margin": lambda d: self.db.put_margin(S.twse_margin(self.f, d)),
            "twse_flows": self._twse_flows,
            "tpex_quotes": lambda d: self.db.put_quotes(S.tpex_quotes(self.f, d)),
            "tpex_inst": lambda d: self.db.put_inst(S.tpex_inst(self.f, d)),
            "tpex_margin": lambda d: self.db.put_margin(S.tpex_margin(self.f, d)),
        }
        for s in STEPS:
            st = run["steps"][s]
            if st.get("status") in DONE:
                continue
            try:
                n = handlers[s](d)
                run["steps"][s] = {"status": "ok", "rows": n, "at": _now()}
            except FetchError as e:
                run["steps"][s] = {"status": "nodata" if e.kind == "nodata" else "failed", "kind": e.kind,
                                   "error": str(e), "url": e.url, "at": _now()}
            except Exception as e:  # 格式改版等非預期錯誤
                log.exception("step %s", s)
                run["steps"][s] = {"status": "failed", "kind": "parse", "error": repr(e), "at": _now()}
            self.db.save_run(run)
            # 第一步就判斷是否休市
            if s == "twse_quotes" and run["steps"][s]["status"] == "nodata" and self._past_publish(d):
                inst = None
                try:
                    S.twse_inst(self.f, d)
                except FetchError as e:
                    inst = e.kind
                if inst == "nodata":
                    run.update(status="holiday", finished_at=_now(), message="交易所無當日資料，判定休市")
                    run["steps"] = {}
                    self.db.save_run(run)
                    self.db.log("info", f"{d} 休市")
                    return run

        if fallback:
            self._fallback(d, run, final)
        ok = [s for s in STEPS if run["steps"][s]["status"] in DONE]
        missing = [s for s in STEPS if s not in ok]
        if not missing:
            run["status"], run["message"] = "complete", "全部資料已抓取"
        elif final:
            run["status"] = "partial" if ok else "failed"
            run["message"] = "缺：" + "、".join(STEP_LABEL[s] for s in missing)
        else:
            hard = [s for s in missing if run["steps"][s]["status"] == "failed"]
            run["status"] = "waiting"
            run["message"] = ("尚未公布：" + "、".join(STEP_LABEL[s] for s in missing if s not in hard)
                              + (("；失敗：" + "、".join(STEP_LABEL[s] for s in hard)) if hard else ""))
        run["finished_at"] = _now()
        self.db.save_run(run)
        self.db.log("warn" if missing else "info", f"{d} {STATUS_TEXT[run['status']]}。{run['message']}")
        if analyze:
            run_analysis(self.db, d)
        return run

    def _past_publish(self, d: str) -> bool:
        n = now_tw()
        return d < n.strftime("%Y%m%d") or n.hour * 60 + n.minute >= 17 * 60

    def _twse_quotes(self, d: str) -> int:
        rows, market = S.twse_quotes(self.f, d)
        if market:
            self.db.put_market(market)
        return self.db.put_quotes(rows)

    def _twse_flows(self, d: str) -> int:
        self.db.put_market(S.twse_flows(self.f, d))
        return 1

    def _fallback(self, d: str, run: dict, final: bool) -> None:
        """官方來源失敗（被擋或格式不符）時，用 FinMind 逐檔補觀察清單。"""
        if not self.cfg.use_finmind_fallback:
            return
        for mkt, steps in (("TWSE", ["twse_quotes", "twse_inst", "twse_margin"]),
                           ("TPEx", ["tpex_quotes", "tpex_inst", "tpex_margin"])):
            bad = [s for s in steps if run["steps"][s]["status"] == "failed"
                   or (final and run["steps"][s]["status"] == "nodata")]
            if not bad:
                continue
            codes = [w["code"] for w in self.db.watchlist() if w["market"] == mkt][: self.cfg.max_watchlist]
            got = {"quotes": 0, "inst": 0, "margin": 0}
            for code in codes:
                try:
                    data = S.finmind_stock(self.f, code, mkt, d, d, self.cfg.finmind_token)
                except FetchError as e:
                    self.db.log("warn", f"FinMind 備援失敗 {code}：{e}")
                    continue
                for key, put in (("quotes", self.db.put_quotes), ("inst", self.db.put_inst), ("margin", self.db.put_margin)):
                    rows = [r for r in data[key] if r["date"] == d]
                    if rows:
                        put(rows)
                        got[key] += 1
            for s in bad:
                key = s.split("_")[1]
                if codes and got[key] == len(codes):
                    run["steps"][s] = {"status": "fallback", "rows": got[key], "source": "FinMind（僅觀察清單）",
                                       "official_error": run["steps"][s].get("error"), "at": _now()}
            self.db.save_run(run)

    # ---------- 補歷史 ----------
    def backfill(self, days: int | None = None) -> None:
        target = days or self.cfg.backfill_days
        start = prev_weekday(today())  # 當天交給每日排程處理，避免融資券尚未公布就被標成「部分缺漏」
        state = {"status": "running", "target": target, "done": 0, "current": None, "started_at": _now()}
        self.db.set_setting("backfill", state)
        self.db.log("info", f"開始補抓歷史資料，目標 {target} 個交易日")
        d, tries, streak_fail = start, 0, 0
        while state["done"] < target and tries < int(target * 1.6) + 10:
            tries += 1
            run = self.db.get_run(d)
            if not run or run["status"] not in ("complete", "partial", "holiday"):
                state["current"] = d
                self.db.set_setting("backfill", state)
                run = self.crawl_day(d, final=True, analyze=False, fallback=False)
            if run["status"] in ("complete", "partial"):
                state["done"] += 1
                streak_fail = 0
            elif run["status"] == "failed":
                streak_fail += 1
                if streak_fail >= 3:
                    state["error"] = "連續 3 天全部抓取失敗（網路不通或被交易所封鎖），已停止補抓，改用 FinMind 補觀察清單"
                    self.db.log("error", state["error"])
                    break
            d = prev_weekday(d)
        for w in self.db.watchlist():
            self.ensure_history(w)
        for dd in self.db.trading_dates(limit=5):
            run_analysis(self.db, dd)
        state.update(status="done", current=None, finished_at=_now())
        self.db.set_setting("backfill", state)
        self.db.log("info", f"歷史資料補抓完成：{state['done']} 個交易日")

    def ensure_history(self, stock: dict, need: int = 61) -> None:
        """單一個股歷史不足（例如官方來源被擋、或剛加入的股票）時，用 FinMind 一次補齊。"""
        n = self.db.one("SELECT COUNT(*) AS n FROM quotes WHERE code=?", (stock["code"],))["n"]
        if n >= need or not self.cfg.use_finmind_fallback:
            return
        end = today()
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=int(need * 1.6) + 20)).strftime("%Y%m%d")
        try:
            data = S.finmind_stock(self.f, stock["code"], stock["market"], start, end, self.cfg.finmind_token)
        except FetchError as e:
            self.db.log("warn", f"{stock['code']} 歷史資料補抓失敗：{e}")
            return
        # 只補官方沒有的日期，避免覆蓋官方資料
        have = {r["date"] for r in self.db.q("SELECT date FROM quotes WHERE code=?", (stock["code"],))}
        self.db.put_quotes([r for r in data["quotes"] if r["date"] not in have])
        have_i = {r["date"] for r in self.db.q("SELECT date FROM inst WHERE code=?", (stock["code"],))}
        self.db.put_inst([r for r in data["inst"] if r["date"] not in have_i])
        have_m = {r["date"] for r in self.db.q("SELECT date FROM margin WHERE code=?", (stock["code"],))}
        self.db.put_margin([r for r in data["margin"] if r["date"] not in have_m])
        self.db.log("info", f"{stock['code']} 已用 FinMind 補齊 {len(data['quotes'])} 天歷史")


class Worker:
    """單一背景執行緒依序處理工作，避免同時對交易所發太多請求。"""

    def __init__(self, crawler: Crawler):
        self.crawler = crawler
        self.q: queue.Queue = queue.Queue()
        self.current: str | None = None
        self.pending: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="worker").start()

    def submit(self, label: str, fn) -> bool:
        with self._lock:
            if label in self.pending or label == self.current:
                return False
            self.pending.append(label)
        self.q.put((label, fn))
        return True

    def _loop(self):
        while True:
            label, fn = self.q.get()
            with self._lock:
                self.pending.remove(label)
                self.current = label
            try:
                fn()
            except Exception:
                log.error("工作失敗 %s\n%s", label, traceback.format_exc())
                self.crawler.db.log("error", f"工作失敗：{label}")
            finally:
                self.current = None

    def state(self) -> dict:
        return {"current": self.current, "pending": list(self.pending)}


class Scheduler:
    """每 30 秒檢查一次台北時間，到點就排入抓取工作。"""

    def __init__(self, crawler: Crawler, worker: Worker):
        self.c = crawler
        self.w = worker
        self.db = crawler.db
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    def slots(self, day: str) -> list[tuple[str, str, bool]]:
        """(觸發時間 key, 目標交易日, 是否最後一次)"""
        out = []
        for hm in self.c.cfg.schedule:
            h = int(hm.split(":")[0])
            if h < 12:  # 清晨：補抓前一交易日，最後一次
                out.append((f"{day} {hm}", prev_weekday(day), True))
            else:
                out.append((f"{day} {hm}", day, False))
        return out

    def next_runs(self) -> list[str]:
        n = now_tw()
        res = []
        for add in range(0, 4):
            day = (n + timedelta(days=add)).strftime("%Y%m%d")
            for key, target, final in self.slots(day):
                t = datetime.strptime(key, "%Y%m%d %H:%M").replace(tzinfo=TW)
                if t > n and not S.is_weekend(target) and not (not final and S.is_weekend(day)):
                    res.append(t.isoformat(timespec="minutes"))
        return sorted(res)[:4]

    def _due(self, startup: bool):
        n = now_tw()
        day = n.strftime("%Y%m%d")
        fired = self.db.get_setting("sched_fired", {}) or {}
        changed = False
        for key, target, final in self.slots(day):
            t = datetime.strptime(key, "%Y%m%d %H:%M").replace(tzinfo=TW)
            if key in fired or t > n:
                continue
            fired[key] = True
            changed = True
            if S.is_weekend(target) or (not final and S.is_weekend(day)):
                continue
            run = self.db.get_run(target)
            if run and run["status"] in ("complete", "holiday") or (run and final and run["status"] == "partial"):
                continue
            # 啟動時錯過的時段只補一次
            self.w.submit(f"抓取 {target}{'（最後一次）' if final else ''}",
                          lambda target=target, final=final: self.c.crawl_day(target, final=final))
        if changed:
            keep = {k: v for k, v in fired.items() if k[:8] >= (n - timedelta(days=3)).strftime("%Y%m%d")}
            self.db.set_setting("sched_fired", keep)

    def _loop(self):
        bf = self.db.get_setting("backfill")
        if not bf or bf.get("status") != "done":
            self.w.submit("補抓歷史資料", self.c.backfill)
        first = True
        while True:
            try:
                self._due(first)
            except Exception:
                log.exception("scheduler")
            first = False
            time.sleep(30)
