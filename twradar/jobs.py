"""抓取流程、補歷史、背景工作佇列與每日排程。"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta

from .analysis import run_analysis
from .config import TW, Config, now_tw
from .dates import is_weekend, prev_weekday, to_date
from .db import DB
from .net import FetchError, Fetcher
from .providers import DATASET_TABLE, Batch, Provider, Transport, build
from .settings import SettingsStore

log = logging.getLogger("twradar.jobs")

DONE = ("ok", "fallback")
STATUS_TEXT = {
    "not_started": "尚未開始抓取", "running": "抓取中", "waiting": "部分資料交易所尚未公布，稍後自動重抓",
    "partial": "已完成抓取，但有資料缺漏", "complete": "抓取完成", "failed": "抓取失敗", "holiday": "休市",
}


def _now() -> str:
    return now_tw().isoformat(timespec="seconds")


def today() -> str:
    return now_tw().strftime("%Y%m%d")


class Crawler:
    """依設定的資料源順序抓取。HTTP（transport）與資料源皆可注入。"""

    def __init__(self, db: DB, cfg: Config, settings: SettingsStore, transport: Transport | None = None,
                 primary: list[Provider] | None = None, fallback: list[Provider] | None = None,
                 weekly: list[Provider] | None = None):
        self.db = db
        self.cfg = cfg
        self.settings = settings
        self._plan = None
        self._custom = primary is not None or fallback is not None or weekly is not None
        self._primary, self._fallback_p, self._weekly_p = primary, fallback, weekly
        self.f = transport or Fetcher(cfg.request_interval, cfg.user_agent)

    # transport 換掉時重建資料源（資料源持有 transport）
    @property
    def f(self) -> Transport:
        return self._f

    @f.setter
    def f(self, transport: Transport) -> None:
        self._f = transport
        self._plan = None
        self.refresh()

    def refresh(self) -> None:
        """設定的資料源有變更時重建（每次抓取前檢查）。"""
        if self._custom:
            if self._plan is not None:
                return
            self.primary, self.fallback = self._primary or [], self._fallback_p or []
            self.weekly = self._weekly_p or []
            self._plan = "custom"
        else:
            plan = (*self.settings.source_plan(self.cfg.env), tuple(self.settings.weekly_sources()))
            if plan == self._plan:
                return
            p, fb, opts, _ = plan
            self.primary, self.fallback = build(p, self._f, opts), build(fb, self._f, opts)
            self.weekly = build(self.settings.weekly_sources(), self._f, opts)
            self._plan = plan
        self.steps = [(f"{pv.id}_{ds}", pv, ds) for pv in self.primary for ds in pv.datasets]
        self.step_ids = [s for s, _, _ in self.steps]
        self.step_labels = {s: pv.step_label(ds) for s, pv, ds in self.steps}

    def _put(self, batch: Batch) -> int:
        n = 0
        for table, rows in batch.items():
            if not rows:
                continue
            if table == "market":
                for r in rows:
                    self.db.put_market(r)
                n += len(rows)
            else:
                n += getattr(self.db, "put_" + table)(rows)
        return n

    # ---------- 單一交易日 ----------
    def crawl_day(self, d: str, final: bool = False, analyze: bool = True, fallback: bool = True) -> dict:
        self.refresh()
        if is_weekend(d):
            run = {"date": d, "status": "holiday", "steps": {}, "attempts": 0, "started_at": _now(),
                   "finished_at": _now(), "message": "週末休市"}
            self.db.save_run(run)
            return run
        run = self.db.get_run(d) or {"date": d, "status": "not_started", "steps": {}, "attempts": 0,
                                     "started_at": None, "finished_at": None, "message": ""}
        if run["status"] == "holiday":
            return run
        run.update(status="running", attempts=run["attempts"] + 1, started_at=_now(), finished_at=None)
        for s in self.step_ids:
            run["steps"].setdefault(s, {"status": "pending"})
        self.db.save_run(run)
        self.db.log("info", f"開始抓取 {d}（第 {run['attempts']} 次{'，最後一次' if final else ''}）")

        for i, (s, pv, ds) in enumerate(self.steps):
            if run["steps"][s].get("status") in DONE:
                continue
            try:
                batch = pv.fetch(ds, d)
                self._put(batch)
                run["steps"][s] = {"status": "ok", "rows": len(batch.get(DATASET_TABLE[ds], [])), "at": _now()}
            except FetchError as e:
                run["steps"][s] = {"status": "nodata" if e.kind == "nodata" else "failed", "kind": e.kind,
                                   "error": str(e), "url": e.url, "at": _now()}
            except Exception as e:  # 格式改版等非預期錯誤
                log.exception("step %s", s)
                run["steps"][s] = {"status": "failed", "kind": "parse", "error": repr(e), "at": _now()}
            self.db.save_run(run)
            # 第一步就判斷是否休市
            if i == 0 and run["steps"][s]["status"] == "nodata" and self._past_publish(d) and pv.confirm_no_trading(d):
                run.update(status="holiday", finished_at=_now(), message="交易所無當日資料，判定休市")
                run["steps"] = {}
                self.db.save_run(run)
                self.db.log("info", f"{d} 休市")
                return run

        if fallback:
            self._fallback(d, run, final)
        self.crawl_weekly(d)
        ok = [s for s in self.step_ids if run["steps"][s]["status"] in DONE]
        missing = [s for s in self.step_ids if s not in ok]
        label = self.step_labels
        if not missing:
            run["status"], run["message"] = "complete", "全部資料已抓取"
        elif final:
            run["status"] = "partial" if ok else "failed"
            run["message"] = "缺：" + "、".join(label[s] for s in missing)
        else:
            hard = [s for s in missing if run["steps"][s]["status"] == "failed"]
            run["status"] = "waiting"
            run["message"] = ("尚未公布：" + "、".join(label[s] for s in missing if s not in hard)
                              + (("；失敗：" + "、".join(label[s] for s in hard)) if hard else ""))
        run["finished_at"] = _now()
        self.db.save_run(run)
        self.db.log("warn" if missing else "info", f"{d} {STATUS_TEXT[run['status']]}。{run['message']}")
        if analyze:
            run_analysis(self.db, d, self.settings)
            if run["status"] in ("complete", "partial"):
                self.track(d)
        return run

    def track(self, d: str) -> None:
        """上線後追蹤：失敗只記錄，不影響晨報。"""
        from . import tracking
        try:
            tracking.daily(self.db, self.settings.get(), d)
        except Exception as e:  # noqa: BLE001
            log.exception("追蹤 %s 失敗", d)
            self.db.log("warn", f"{d} 模型追蹤失敗：{e!r}")

    def crawl_weekly(self, d: str) -> str | None:
        """集保股權分散表：上次資料距今滿 7 天才抓（每週一次）；失敗只記錄，不影響當日狀態。"""
        if not self.weekly:
            return None
        last = self.db.holders_latest()
        if last and (to_date(d) - to_date(last)).days < 7:
            return None
        codes = {w["code"] for w in self.settings.watchlist()}
        for pv in self.weekly:
            try:
                rows = pv.fetch_week(codes)["holders"]
            except FetchError as e:
                self.db.log("warn", f"{pv.label}股權分散表抓取失敗：{e}")
                continue
            except Exception as e:  # 格式改版
                log.exception("weekly %s", pv.id)
                self.db.log("warn", f"{pv.label}股權分散表格式無法解析：{e!r}")
                continue
            got = max((r["date"] for r in rows), default=None)
            if not got or (last and got <= last):
                self.db.log("info", f"{pv.label}股權分散表尚未更新（最新 {got or '—'}），下次再試")
                return None
            self.db.put_holders(rows)
            self.db.log("info", f"已取得 {got} 集保股權分散表（{len({r['code'] for r in rows})} 檔）")
            return got
        return None

    def _past_publish(self, d: str) -> bool:
        n = now_tw()
        return d < n.strftime("%Y%m%d") or n.hour * 60 + n.minute >= 17 * 60

    def _fallback(self, d: str, run: dict, final: bool) -> None:
        """全市場來源失敗（被擋或格式不符）時，用逐檔資料源補觀察清單。"""
        if not self.fallback:
            return
        watch = self.settings.watchlist()
        for pv in self.primary:
            for mkt in pv.markets:
                bad = [(s, ds) for s, p, ds in self.steps if p is pv
                       and any(ds in fb.datasets for fb in self.fallback)
                       and (run["steps"][s]["status"] == "failed" or (final and run["steps"][s]["status"] == "nodata"))]
                if not bad:
                    continue
                want = tuple(ds for _, ds in bad)
                codes = [w["code"] for w in watch if w["market"] == mkt][: self.cfg.max_watchlist]
                got = {ds: 0 for ds in want}
                used: dict[str, str] = {}
                for code in codes:
                    for fb in self.fallback:
                        sub = tuple(ds for ds in want if ds in fb.datasets)
                        try:
                            data = fb.fetch_stock(code, mkt, d, d, sub)
                        except FetchError as e:
                            self.db.log("warn", f"{fb.label} 備援失敗 {code}：{e}")
                            continue
                        for ds in sub:
                            table = DATASET_TABLE[ds]
                            rows = [r for r in data.get(table, []) if r["date"] == d]
                            if rows:
                                self._put({table: rows})
                                got[ds] += 1
                                used[ds] = fb.label
                        break
                for s, ds in bad:
                    if codes and got[ds] == len(codes):
                        run["steps"][s] = {"status": "fallback", "rows": got[ds], "source": f"{used[ds]}（僅觀察清單）",
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
        t0, fetched = time.monotonic(), 0
        while state["done"] < target and tries < int(target * 1.6) + 10:
            tries += 1
            run = self.db.get_run(d)
            cached = bool(run and run["status"] in ("complete", "partial", "holiday"))
            if not cached:
                state["current"] = d
                self.db.set_setting("backfill", state)
                run = self.crawl_day(d, final=True, analyze=False, fallback=False)
                fetched += 1
            if run["status"] in ("complete", "partial"):
                state["done"] += 1
                streak_fail = 0
            if not cached:
                left = target - state["done"]
                eta = (time.monotonic() - t0) / fetched * left * 1.05 / 60 if fetched else 0
                log.info("補抓 %s：%s（%d/%d，預估還要 %d 分鐘）", d, STATUS_TEXT.get(run["status"], run["status"]),
                         state["done"], target, round(eta))
            elif run["status"] == "failed":
                streak_fail += 1
                if streak_fail >= 3:
                    state["error"] = "連續 3 天全部抓取失敗（網路不通或被交易所封鎖），已停止補抓，改用 FinMind 補觀察清單"
                    self.db.log("error", state["error"])
                    break
            d = prev_weekday(d)
        for w in self.settings.watchlist():
            self.ensure_history(w)
        for dd in self.db.trading_dates(limit=5):
            run_analysis(self.db, dd, self.settings)
        state.update(status="done", current=None, finished_at=_now())
        self.db.set_setting("backfill", state)
        self.db.log("info", f"歷史資料補抓完成：{state['done']} 個交易日")

    def ensure_history(self, stock: dict, need: int = 61) -> None:
        """單一個股歷史不足（例如官方來源被擋、或剛加入的股票）時，用逐檔資料源一次補齊。"""
        n = self.db.one("SELECT COUNT(*) AS n FROM quotes WHERE code=?", (stock["code"],))["n"]
        if n >= need or not self.fallback:
            return
        end = today()
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=int(need * 1.6) + 20)).strftime("%Y%m%d")
        for fb in self.fallback:
            try:
                data = fb.fetch_stock(stock["code"], stock["market"], start, end)
            except FetchError as e:
                self.db.log("warn", f"{stock['code']} 歷史資料補抓失敗（{fb.label}）：{e}")
                continue
            # 只補官方沒有的日期，避免覆蓋官方資料
            for table in ("quotes", "inst", "margin"):
                have = {r["date"] for r in self.db.q(f"SELECT date FROM {table} WHERE code=?", (stock["code"],))}
                self._put({table: [r for r in data.get(table, []) if r["date"] not in have]})
            self.db.log("info", f"{stock['code']} 已用 {fb.label} 補齊 {len(data.get('quotes', []))} 天歷史")
            return


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
        for hm in self.c.settings.schedule():
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
                if t > n and not is_weekend(target) and not (not final and is_weekend(day)):
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
            if is_weekend(target) or (not final and is_weekend(day)):
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
