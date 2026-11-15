"""全市場強勢股掃描，以及它的回溯驗證。

篩選（每天、只用當天以前的資料）
- 一般股票（4 碼數字，排除 ETF、存託憑證），至少 61 個交易日資料。
- 流動性：近 20 日平均成交金額 ≥ minValue（預設 1 億元）。
- 趨勢：收盤 > 月線 > 季線，月線走平或上揚，收盤在近 60 日最高價的 90% 以上。
- 不追高（部分方法）：今天的 5 日乖離排在這檔自己近 250 日的前 10% → 排除。

排序方法
- 相對強度：近 20 日、60 日漲幅減去加權指數同期漲幅，各取百分位後平均。
- 法人：外資＋投信近 5 日累計買超 ÷ 近 5 日成交量，取百分位。
- 綜合：相對強度 70%、法人 30%。

驗證：每天取前 N 名，隔日開盤買、持有 h 日後收盤（還原價），和兩個基準比較：
「全市場」（通過流動性的所有股票）與「趨勢股」（通過趨勢條件的所有股票，看排序本身有沒有加分）。
同一天的股票彼此相關，所以每天先算（前 N 名平均 − 基準平均），再看這些日差距的平均、標準誤與勝出天數比例。
"""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass

from .analysis import adjust_prices
from .backtest import _is_stock
from .db import DB

HORIZONS = (5, 10, 20)
METHODS = ("相對強度", "相對強度＋不追高", "法人", "綜合", "綜合＋不追高")


@dataclass
class ScanConfig:
    top: int = 20
    min_value: float = 1e8          # 元
    near_high: float = 0.9
    bias_pct: float = 90            # 5 日乖離百分位上限（不追高）
    bias_window: int = 250


class Series:
    __slots__ = ("code", "name", "market", "dates", "open", "close", "value", "vol", "inst", "pos",
                 "ma5", "ma20", "ma60", "max60", "val20", "inst5", "vol5", "biasPct")

    def __init__(self, code, name, market, rows, inst: dict, trading, intern: dict | None = None):
        self.code, self.name, self.market = code, name, market
        adj, _ = adjust_prices(list(reversed(rows)), trading)
        adj.reverse()
        intern = intern if intern is not None else {}
        self.dates = [intern.setdefault(r["date"], r["date"]) for r in rows]
        self.open = [a["open"] for a in adj]
        self.close = [a["close"] for a in adj]
        self.value = [r["value"] or 0.0 for r in rows]
        self.vol = [r["volume"] or 0.0 for r in rows]
        self.inst = [inst.get(d) for d in self.dates]
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self._derive()
        self.value = self.vol = self.inst = None   # 衍生完就不需要，省記憶體

    @staticmethod
    def _ma(xs, n):
        out, s, q = [None] * len(xs), 0.0, []
        for i, x in enumerate(xs):
            q.append(x)
            s += x
            if len(q) > n:
                s -= q.pop(0)
            if len(q) == n:
                out[i] = s / n
        return out

    def _derive(self, bias_window: int = 250):
        c = self.close
        self.ma5, self.ma20, self.ma60 = self._ma(c, 5), self._ma(c, 20), self._ma(c, 60)
        self.val20 = self._ma(self.value, 20)
        self.vol5 = self._ma(self.vol, 5)
        ins = [x if x is not None else 0.0 for x in self.inst]
        has = self._ma([1.0 if x is not None else 0.0 for x in self.inst], 5)
        s5 = self._ma(ins, 5)
        self.inst5 = [s * 5 if s is not None and h == 1.0 else None for s, h in zip(s5, has)]
        self.max60 = [None] * len(c)
        dq: list[int] = []
        for i, x in enumerate(c):
            while dq and c[dq[-1]] <= x:
                dq.pop()
            dq.append(i)
            if dq[0] <= i - 60:
                dq.pop(0)
            if i >= 59:
                self.max60[i] = c[dq[0]]
        # 5 日乖離在自己過去 bias_window 日的百分位
        self.biasPct = [None] * len(c)
        win: list[float] = []
        hist: list[float] = []
        for i in range(len(c)):
            b = c[i] / self.ma5[i] - 1 if self.ma5[i] else None
            if b is None:
                continue
            if len(win) >= 60:
                self.biasPct[i] = bisect.bisect_left(win, b) / len(win) * 100
            bisect.insort(win, b)
            hist.append(b)
            if len(hist) > bias_window:
                win.pop(bisect.bisect_left(win, hist.pop(0)))

    def fwd(self, i: int) -> dict[int, float]:
        if i + 1 >= len(self.dates):
            return {}
        entry = self.open[i + 1] or self.close[i + 1]
        return {h: (self.close[i + h] / entry - 1) * 100 for h in HORIZONS if i + h < len(self.dates) and entry}


def load(db: DB, until: str, since: str = "", min_value: float = 0) -> tuple[dict[str, Series], dict[str, float], list[str]]:
    """全市場一般股票的序列（還原價）、加權指數、交易日。期間內從未達到流動性門檻的股票不載入（省記憶體）。"""
    trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=? ORDER BY date",
                                       (since, until))]
    taiex = {r["date"]: r["taiex"] for r in db.q("SELECT date, taiex FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=?",
                                                 (since, until))}
    codes = [r["code"] for r in db.q("SELECT DISTINCT code FROM quotes WHERE date>=? AND date<=?", (since, until))
             if _is_stock(r["code"])]
    out: dict[str, Series] = {}
    intern = {d: d for d in trading}
    for code in codes:   # 逐檔讀取：全市場一次讀進來會用掉好幾百 MB 記憶體
        rows = [dict(r) for r in db.q("SELECT date, name, market, open, high, low, close, change, volume, value FROM quotes "
                                      "WHERE code=? AND date>=? AND date<=? AND close IS NOT NULL ORDER BY date",
                                      (code, since, until))]
        if len(rows) < 61:
            continue
        inst = {r["date"]: (r["foreign_net"] or 0) + (r["trust_net"] or 0)
                for r in db.q("SELECT date, foreign_net, trust_net FROM inst WHERE code=? AND date>=? AND date<=?",
                              (code, since, until)) if r["foreign_net"] is not None or r["trust_net"] is not None}
        if min_value and max((r["value"] or 0) for r in rows) < min_value:
            continue
        sr = Series(code, rows[-1]["name"], rows[-1]["market"], rows, inst, trading, intern)
        if min_value and max((v for v in sr.val20 if v is not None), default=0) < min_value:
            continue
        out[code] = sr
    return out, taiex, trading


def _pct_rank(vals: dict[str, float]) -> dict[str, float]:
    ks = sorted(vals, key=lambda k: vals[k])
    n = len(ks)
    return {k: (i + 0.5) / n * 100 for i, k in enumerate(ks)} if n else {}


def screen(data: dict[str, Series], taiex: dict, trading: list[str], d: str, cfg: ScanConfig) -> dict:
    """d 當天：{"liquid": [代號], "trend": [代號], "rows": {代號: 指標}, "rank": {方法: [代號...]}}。"""
    ti = bisect.bisect_left(trading, d)
    if ti >= len(trading) or trading[ti] != d or ti < 60:
        return {"liquid": [], "trend": [], "rows": {}, "rank": {m: [] for m in METHODS}}
    t20, t60 = taiex[d] / taiex[trading[ti - 20]] - 1, taiex[d] / taiex[trading[ti - 60]] - 1
    liquid, trend, rows = [], [], {}
    for code, s in data.items():
        i = s.pos.get(d)
        if i is None or i < 60 or s.val20[i] is None or s.val20[i] < cfg.min_value:
            continue
        liquid.append(code)
        c, m20, m20p, m60, hi = s.close[i], s.ma20[i], s.ma20[i - 1], s.ma60[i], s.max60[i]
        if None in (c, m20, m20p, m60, hi) or not (c > m20 > m60 and m20 >= m20p and c >= hi * cfg.near_high):
            continue
        trend.append(code)
        v5 = s.vol5[i]
        rows[code] = {"rs20": (c / s.close[i - 20] - 1 - t20) * 100, "rs60": (c / s.close[i - 60] - 1 - t60) * 100,
                      "inst5Pct": None if s.inst5[i] is None or not v5 else s.inst5[i] / (v5 * 5) * 100,
                      "biasPct": s.biasPct[i], "bias5": (c / s.ma5[i] - 1) * 100 if s.ma5[i] else None,
                      "maDist": (c / m20 - 1) * 100, "value20": s.val20[i] / 1e8, "close": c}
    r20 = _pct_rank({k: v["rs20"] for k, v in rows.items()})
    r60 = _pct_rank({k: v["rs60"] for k, v in rows.items()})
    ri = _pct_rank({k: v["inst5Pct"] for k, v in rows.items() if v["inst5Pct"] is not None})
    sc_rs = {k: (r20[k] + r60[k]) / 2 for k in rows}
    sc_mix = {k: 0.7 * sc_rs[k] + 0.3 * ri.get(k, 50) for k in rows}
    hot = {k for k, v in rows.items() if v["biasPct"] is not None and v["biasPct"] >= cfg.bias_pct}
    top = lambda sc, ex=(): [k for k in sorted(sc, key=lambda k: -sc[k]) if k not in ex][:cfg.top]
    for k in rows:
        rows[k].update(rsScore=round(sc_rs[k], 1), instScore=round(ri[k], 1) if k in ri else None, score=round(sc_mix[k], 1),
                       hot=k in hot)
    return {"liquid": liquid, "trend": trend, "rows": rows,
            "rank": {"相對強度": top(sc_rs), "相對強度＋不追高": top(sc_rs, hot), "法人": top(ri),
                     "綜合": top(sc_mix), "綜合＋不追高": top(sc_mix, hot)}}


def _regime(taiex, trading, d) -> str:
    i = bisect.bisect_right(trading, d)
    xs = [taiex[x] for x in reversed(trading[max(0, i - 21):i])]
    if len(xs) < 21:
        return "未知"
    ma, prev = sum(xs[:20]) / 20, sum(xs[1:21]) / 20
    if xs[0] >= ma and ma >= prev:
        return "多頭"
    if xs[0] < ma and ma < prev:
        return "空頭"
    return "盤整"


def _stats(diffs: list[float], ns: list[int]) -> dict:
    if not diffs:
        return {"days": 0}
    out = {"days": len(diffs), "excess": round(statistics.fmean(diffs), 2),
           "hit": round(sum(1 for x in diffs if x > 0) / len(diffs) * 100, 1), "avgN": round(statistics.fmean(ns), 1)}
    if len(diffs) >= 2:
        out["se"] = round(statistics.stdev(diffs) / math.sqrt(len(diffs)), 2)
    return out


def study(db: DB, dates: list[str], split: str, cfg: ScanConfig | None = None, progress=None) -> dict:
    """每天前 N 名，相對「全市場」與「趨勢股」的超額報酬。"""
    cfg = cfg or ScanConfig()
    data, taiex, trading = load(db, dates[-1], min_value=cfg.min_value)
    # (方法, 基準, 樣本或環境, h) → [日差距], [前 N 名檔數]
    acc: dict[tuple, tuple[list, list]] = {}
    cnt = {"liquid": [], "trend": []}
    for k, d in enumerate(dates):
        sc = screen(data, taiex, trading, d, cfg)
        if not sc["trend"]:
            continue
        cnt["liquid"].append(len(sc["liquid"]))
        cnt["trend"].append(len(sc["trend"]))
        fw = {c: data[c].fwd(data[c].pos[d]) for c in sc["liquid"]}
        segs = ("樣本外" if d >= split else "樣本內", "全期", _regime(taiex, trading, d))
        for h in HORIZONS:
            base = {"全市場": [fw[c][h] for c in sc["liquid"] if h in fw[c]],
                    "趨勢股": [fw[c][h] for c in sc["trend"] if h in fw[c]]}
            if not base["全市場"] or not base["趨勢股"]:
                continue
            bm = {b: statistics.fmean(v) for b, v in base.items()}
            for m, codes in sc["rank"].items():
                xs = [fw[c][h] for c in codes if h in fw[c]]
                if not xs:
                    continue
                for b, v in bm.items():
                    for sg in segs:
                        a = acc.setdefault((m, b, sg, h), ([], []))
                        a[0].append(statistics.fmean(xs) - v)
                        a[1].append(len(xs))
        if progress:
            progress(k + 1, len(dates))
    out = {"config": {"top": cfg.top, "minValue": cfg.min_value, "nearHigh": cfg.near_high, "biasPct": cfg.bias_pct},
           "stocks": len(data), "avgLiquid": round(statistics.fmean(cnt["liquid"]), 1) if cnt["liquid"] else 0,
           "avgTrend": round(statistics.fmean(cnt["trend"]), 1) if cnt["trend"] else 0, "methods": {}}
    for m in METHODS:
        out["methods"][m] = {b: {sg: {f"f{h}": _stats(*acc.get((m, b, sg, h), ([], []))) for h in HORIZONS}
                                 for sg in ("樣本內", "樣本外", "全期", "多頭", "盤整", "空頭")}
                             for b in ("全市場", "趨勢股")}
    return out


DEFAULT_METHOD = "法人"   # 回溯中唯一樣本內、樣本外都勝過基準的排序


def latest(db: DB, d: str | None = None, method: str = DEFAULT_METHOD, cfg: ScanConfig | None = None) -> dict:
    """d 當天（預設最新交易日）的掃描結果。"""
    return snapshot(db, d, cfg, (method,))["methods"][method] | {"method": method}


def snapshot(db: DB, d: str | None = None, cfg: ScanConfig | None = None, methods=METHODS) -> dict:
    """d 當天所有排序方法的名單：{"date", "liquid", "trend", "regime", "methods": {方法: {"date", "liquid", "trend", "rows"}}}。"""
    cfg = cfg or ScanConfig()
    for method in methods:
        if method not in METHODS:
            raise ValueError(f"未知的方法：{method}（可用：{'、'.join(METHODS)}）")
    last = db.one("SELECT MAX(date) AS d FROM market WHERE taiex IS NOT NULL")["d"]
    d = d or last
    if not d:
        raise ValueError("還沒有任何行情資料")
    since = db.one("SELECT MIN(date) AS d FROM (SELECT date FROM market WHERE taiex IS NOT NULL AND date<=? "
                   "ORDER BY date DESC LIMIT 320)", (d,))["d"] or ""
    data, taiex, trading = load(db, d, since, cfg.min_value)
    sc = screen(data, taiex, trading, d, cfg)
    def rows(method):
        return [{"code": c, "name": data[c].name, "market": data[c].market,
                 **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in sc["rows"][c].items()}}
                for c in sc["rank"][method]]
    head = {"date": d, "liquid": len(sc["liquid"]), "trend": len(sc["trend"])}
    return {**head, "regime": _regime(taiex, trading, d), "top": cfg.top,
            "methods": {m: {**head, "rows": rows(m)} for m in methods}}
