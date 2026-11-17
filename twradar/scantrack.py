"""全市場清單的上線後追蹤：從上線那天起，每天記下流動性合格的股票池與各排序方法的前 N 名，之後回填報酬。

回溯的樣本外期間已經被用來挑選預設排序（法人），不再是「沒看過的資料」；
真正的檢驗只剩上線後的每一天，所以這裡**不補建過去**，只從開始記錄的那天往後累積。

- 報酬定義與回溯相同：隔日開盤買、持有 h 日後收盤（還原價、未扣費用）。
- 每天：前 N 名平均 − 全市場（流動性合格）平均；再看這些日差距的平均、標準誤、勝出天數比例。
"""
from __future__ import annotations

import json
import math
import statistics

from .backtest import HORIZONS, History
from .db import DB


def _trading(db: DB) -> list[str]:
    return [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]


def record(db: DB, d: str, cfg=None) -> int:
    """記錄 d 當天（交易日）的股票池與各方法前 N 名；同一天重跑會覆蓋。"""
    from . import scan as SC
    cfg = cfg or SC.ScanConfig()
    trading = _trading(db)
    if d not in trading:
        return 0
    i = trading.index(d)
    since = trading[max(0, i - 320)]
    data, taiex, tr = SC.load(db, d, since, cfg.min_value)
    sc = SC.screen(data, taiex, tr, d, cfg)
    ranks: dict[str, dict[str, int]] = {}
    for m, codes in sc["rank"].items():
        for k, c in enumerate(codes):
            ranks.setdefault(c, {})[m] = k + 1
    trend = set(sc["trend"])
    rows = [(d, c, 1 if c in trend else 0, json.dumps(ranks.get(c, {}), ensure_ascii=False)) for c in sc["liquid"]]
    with db.tx() as cx:
        cx.execute("DELETE FROM scan_track WHERE date=?", (d,))
        cx.executemany("INSERT INTO scan_track(date, code, trend, ranks) VALUES(?,?,?,?)", rows)
    return len(rows)


def update_forward(db: DB, until: str | None = None) -> int:
    trading = _trading(db)
    if not trading:
        return 0
    until = until or trading[-1]
    i = trading.index(until) if until in trading else len(trading) - 1
    lo = trading[max(0, i - 30)]
    pend = db.q("SELECT date, code, f5, f10, f20 FROM scan_track WHERE f20 IS NULL AND date>=? AND date<?", (lo, until))
    if not pend:
        return 0
    hist = History(db, sorted({r["code"] for r in pend}), until, since=min(r["date"] for r in pend))
    upd = []
    for r in pend:
        fw = hist.fwd(r["code"], r["date"])
        vals = [round(fw[h], 4) if h in fw else r[f"f{h}"] for h in HORIZONS]
        if vals != [r[f"f{h}"] for h in HORIZONS]:
            upd.append((*vals, r["date"], r["code"]))
    with db.tx() as cx:
        cx.executemany("UPDATE scan_track SET f5=?, f10=?, f20=? WHERE date=? AND code=?", upd)
    return len(upd)


def _stats(diffs: list[float]) -> dict:
    if not diffs:
        return {"days": 0}
    out = {"days": len(diffs), "excess": round(statistics.fmean(diffs), 2),
           "hit": round(sum(1 for x in diffs if x > 0) / len(diffs) * 100, 1)}
    if len(diffs) >= 2:
        out["se"] = round(statistics.stdev(diffs) / math.sqrt(len(diffs)), 2)
    return out


def summary(db: DB) -> dict:
    head = db.one("SELECT MIN(date) AS a, MAX(date) AS b, COUNT(DISTINCT date) AS n FROM scan_track")
    if not head or not head["n"]:
        return {"since": None, "days": 0, "methods": {}}
    by_day: dict[str, list] = {}
    for r in db.q("SELECT date, trend, ranks, f5, f10, f20 FROM scan_track WHERE f5 IS NOT NULL"):
        by_day.setdefault(r["date"], []).append(r)
    acc: dict[tuple, list[float]] = {}
    for d, rows in by_day.items():
        for h in HORIZONS:
            base = [r[f"f{h}"] for r in rows if r[f"f{h}"] is not None]
            trend = [r[f"f{h}"] for r in rows if r[f"f{h}"] is not None and r["trend"]]
            if not base:
                continue
            bm, tm = statistics.fmean(base), (statistics.fmean(trend) if trend else None)
            tops: dict[str, list[float]] = {}
            for r in rows:
                if r[f"f{h}"] is None:
                    continue
                for m in json.loads(r["ranks"] or "{}"):
                    tops.setdefault(m, []).append(r[f"f{h}"])
            for m, xs in tops.items():
                acc.setdefault((m, "全市場", h), []).append(statistics.fmean(xs) - bm)
                if tm is not None:
                    acc.setdefault((m, "趨勢股", h), []).append(statistics.fmean(xs) - tm)
    methods: dict = {}
    for (m, b, h), xs in acc.items():
        methods.setdefault(m, {}).setdefault(b, {})[f"f{h}"] = _stats(xs)
    return {"since": head["a"], "latest": head["b"], "days": head["n"], "methods": methods}


def refresh(db: DB) -> dict:
    s = summary(db)
    db.set_setting("scanTracking", s)
    return s


def cached(db: DB) -> dict:
    return db.get_setting("scanTracking") or refresh(db)


def daily(db: DB, d: str) -> int:
    n = record(db, d)
    update_forward(db, d)
    refresh(db)
    return n
