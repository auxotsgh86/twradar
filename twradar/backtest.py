"""回溯分析：用資料庫裡的歷史資料，逐日重播判定規則，檢驗訊號出現後的實際走勢。

原則
- 不偷看未來：每一天只用「當天以前」的資料計算指標與訊號。
- 盤後系統：訊號隔一個交易日以開盤價成交，扣手續費與證交稅。
- 價格一律用還原股價（除權息、減資），報酬才不會被除息跳空扭曲。
- 股票池不用「今天的成份股名單」（倖存者偏差），而是每季依「當時」前 60 日平均成交金額排名：
    上市前 50 名＝大型、上市 51～150 名＝中型、上櫃前 50 名＝上櫃；另加觀察清單。
- 依大盤環境（多頭／空頭／盤整）、樣本內／樣本外、每半年分段統計，檢查規則是否穩定。

限制
- 集保股權分散表只有接入後的資料，大戶／散戶週增減在回溯期間多半為「資料不足」。
- 不模擬「連續 N 日確認」（預設規則的停損等都是當日執行）。
"""
from __future__ import annotations

import bisect
import copy
import itertools
import json
import math
import statistics
from dataclasses import dataclass, field

from .analysis import DEFAULT_RULES, _indicators, adjust_prices, evaluate
from .db import DB

HORIZONS = (5, 10, 20)
# 指標計算用到的參數：改了要重算指標，不能放進參數組合
INDICATOR_PARAMS = ("pullbackDays", "whaleLots", "whaleAmount", "retailMaxLots")


@dataclass
class BTConfig:
    start: str                      # YYYYMMDD（含）
    end: str                        # YYYYMMDD（含）
    split: str                      # 此日（含）之後為樣本外
    universe: str = "auto"          # auto | watchlist | 檔案路徑（CSV：code[,group]）
    top_large: int = 50
    top_mid: int = 150
    top_otc: int = 50
    max_adds: int = 2
    variants: list[tuple[str, dict]] = field(default_factory=lambda: [("目前設定", {})])


# ---------------- 股票池 ----------------

def _is_stock(code: str) -> bool:
    """一般股票：4 碼數字，排除 ETF（00 開頭）與存託憑證（91 開頭）。"""
    return len(code) == 4 and code.isdigit() and not code.startswith("00") and not code.startswith("91")


def is_financial(code: str) -> bool:
    return code[:2] in ("28", "58")


def quarter_starts(dates: list[str]) -> list[str]:
    """每季第一個交易日（含第一天）。"""
    out, seen = [], set()
    for d in dates:
        q = (d[:4], (int(d[4:6]) - 1) // 3)
        if q not in seen:
            seen.add(q)
            out.append(d)
    return out


def rank_universe(db: DB, d: str, cfg: BTConfig) -> dict[str, set[str]]:
    """以 d 之前 60 個交易日的平均成交金額排名（時點正確）。回傳 {代號: {標籤}}。"""
    ds = [r["date"] for r in db.q("SELECT date FROM market WHERE date<? AND taiex IS NOT NULL ORDER BY date DESC LIMIT 60", (d,))]
    if not ds:
        return {}
    rows = db.q("SELECT code, market, AVG(value) AS v, COUNT(*) AS n FROM quotes WHERE date>=? AND date<=? AND value IS NOT NULL "
                "GROUP BY code, market", (min(ds), max(ds)))
    need = max(20, len(ds) // 2)
    tw = sorted([r for r in rows if r["market"] == "TWSE" and _is_stock(r["code"]) and r["n"] >= need], key=lambda r: -r["v"])
    otc = sorted([r for r in rows if r["market"] == "TPEx" and _is_stock(r["code"]) and r["n"] >= need], key=lambda r: -r["v"])
    out: dict[str, set[str]] = {}
    for i, r in enumerate(tw[: cfg.top_mid]):
        out.setdefault(r["code"], set()).add("大型" if i < cfg.top_large else "中型")
    for r in otc[: cfg.top_otc]:
        out.setdefault(r["code"], set()).add("上櫃")
    return out


def build_universe(db: DB, dates: list[str], cfg: BTConfig, watchlist: list[dict]) -> dict[str, dict[str, set[str]]]:
    """每個交易日的股票池 {日期: {代號: {標籤}}}。"""
    fixed: dict[str, set[str]] = {}
    if cfg.universe == "watchlist":
        fixed = {w["code"]: {"觀察清單"} for w in watchlist}
    elif cfg.universe != "auto":
        with open(cfg.universe, encoding="utf-8-sig") as f:
            for line in f:
                parts = [x.strip() for x in line.split(",")]
                if parts and parts[0] and parts[0][0].isdigit():
                    fixed.setdefault(parts[0], set()).add(parts[1] if len(parts) > 1 and parts[1] else "自訂")
    out: dict[str, dict[str, set[str]]] = {}
    if cfg.universe == "auto":
        cur: dict[str, set[str]] = {}
        qs = set(quarter_starts(dates))
        for d in dates:
            if d in qs or not cur:
                cur = rank_universe(db, d, cfg)
                for w in watchlist:
                    cur.setdefault(w["code"], set()).add("觀察清單")
            out[d] = cur
    else:
        for d in dates:
            out[d] = fixed
    return out


# ---------------- 歷史資料（一次載入記憶體） ----------------

class History:
    def __init__(self, db: DB, codes: list[str], until: str):
        self.q: dict[str, list[dict]] = {c: [] for c in codes}
        self.ins: dict[str, list[dict]] = {c: [] for c in codes}
        self.mh: dict[str, list[dict]] = {c: [] for c in codes}
        ph = ",".join("?" * len(codes)) or "''"
        for r in db.q(f"SELECT code, date, open, high, low, close, change, volume, value, src FROM quotes "
                      f"WHERE code IN ({ph}) AND date<=? ORDER BY code, date", (*codes, until)):
            self.q[r["code"]].append(dict(r))
        for r in db.q(f"SELECT code, date, foreign_net, trust_net, dealer_net FROM inst WHERE code IN ({ph}) AND date<=? "
                      f"ORDER BY code, date", (*codes, until)):
            self.ins[r["code"]].append(dict(r))
        for r in db.q(f"SELECT code, date, margin_prev, margin_bal, short_prev, short_bal FROM margin WHERE code IN ({ph}) "
                      f"AND date<=? ORDER BY code, date", (*codes, until)):
            self.mh[r["code"]].append(dict(r))
        self.qd = {c: [r["date"] for r in v] for c, v in self.q.items()}
        self.raw = {c: {r["date"]: r for r in v} for c, v in self.q.items()}
        self.id_ = {c: [r["date"] for r in v] for c, v in self.ins.items()}
        self.md = {c: [r["date"] for r in v] for c, v in self.mh.items()}
        self.trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date<=? ORDER BY date", (until,))]
        self.taiex = {r["date"]: r["taiex"] for r in db.q("SELECT date, taiex FROM market WHERE taiex IS NOT NULL AND date<=?", (until,))}
        # 全期間還原價（只供計算報酬；指標仍用「當時」的資料計算）
        self.adj: dict[str, dict[str, dict]] = {}
        for c, rows in self.q.items():
            adj, _ = adjust_prices(list(reversed(rows)), self.trading)
            self.adj[c] = {r["date"]: r for r in adj}

    def _tail(self, rows, dates, d, n):
        i = bisect.bisect_right(dates, d)
        return list(reversed(rows[max(0, i - n):i]))

    def rec(self, code: str, d: str, P: dict) -> dict:
        q = self._tail(self.q[code], self.qd[code], d, 61)
        ins = self._tail(self.ins[code], self.id_[code], d, 30)
        mh = self._tail(self.mh[code], self.md[code], d, 60)
        m = mh[0] if mh and mh[0]["date"] == d else None
        i = bisect.bisect_right(self.trading, d)
        trading = self.trading[max(0, i - 90):i]
        src = {r["src"] for r in q[:1] if r.get("src") and r["date"] == d}
        rec = _indicators(q, ins, m, src or None, d, mh, P, trading or None)
        if rec:
            rec.update(self.market(d))
        return rec

    def market(self, d: str) -> dict:
        i = bisect.bisect_right(self.trading, d)
        xs = [self.taiex[x] for x in reversed(self.trading[max(0, i - 21):i])]
        out = {}
        if xs:
            out["mktClose"] = xs[0]
        if len(xs) >= 20:
            out["mktMa20"] = sum(xs[:20]) / 20
        if len(xs) >= 21:
            out["mktMa20Prev"] = sum(xs[1:21]) / 20
        return out

    def regime(self, d: str) -> str:
        m = self.market(d)
        if "mktMa20Prev" not in m:
            return "未知"
        up = m["mktMa20"] >= m["mktMa20Prev"]
        if m["mktClose"] >= m["mktMa20"] and up:
            return "多頭"
        if m["mktClose"] < m["mktMa20"] and not up:
            return "空頭"
        return "盤整"

    def factor(self, code: str, d: str) -> float | None:
        """還原價 ÷ 原始價（當日）：把成本換算成「當日原始價」時使用。"""
        r, raw = self.adj[code].get(d), self.raw[code].get(d)
        if not r or not raw or not raw["close"]:
            return None
        return r["close"] / raw["close"]

    def fwd(self, code: str, d: str) -> dict[int, float]:
        """訊號日 d 之後：隔日開盤買進，持有 h 個交易日後收盤的報酬（%，還原價，未扣費用）。"""
        ds = self.qd[code]
        i = bisect.bisect_right(ds, d)
        if i >= len(ds):
            return {}
        a = self.adj[code]
        entry = a[ds[i]]["open"] or a[ds[i]]["close"]
        out = {}
        for h in HORIZONS:
            j = i + h - 1
            if j < len(ds) and entry and a[ds[j]]["close"]:
                out[h] = (a[ds[j]]["close"] / entry - 1) * 100
        return out


# ---------------- 交易模擬 ----------------

@dataclass
class Position:
    entry_date: str
    tags: list[str]
    shares: float = 0.0
    bought: float = 0.0        # 累計買進股數（還原價計）
    invested: float = 0.0
    proceeds: float = 0.0
    adds: int = 0
    reduced: bool = False
    peak: float = 0.0          # 還原價
    low: float = math.inf      # 還原價
    entry_rule: str = ""

    @property
    def avg(self) -> float:
        """平均買進成本（還原價，含手續費）。減碼不改變成本。"""
        return self.invested / self.bought if self.bought else 0.0


def simulate(hist: History, code: str, dates: list[str], universe: dict, P: dict, S: dict, cfg: BTConfig,
             rec_cache: dict, events: list | None, regimes: dict) -> list[dict]:
    fee, tax = P.get("feePct", 0.1425) / 100, P.get("taxPct", 0.3) / 100
    trades: list[dict] = []
    pos: Position | None = None
    pending: tuple | None = None
    adj = hist.adj[code]
    for d in dates:
        bar = adj.get(d)
        if bar is None:
            continue
        # 執行前一日的訊號（開盤價）
        if pending:
            px = bar["open"] or bar["close"]
            kind = pending[0]
            if kind in ("buy", "add"):
                if kind == "buy":
                    pos = Position(d, pending[1], entry_rule=pending[2])
                else:
                    pos.adds += 1
                pos.shares += 1 / (px * (1 + fee))
                pos.bought += 1 / (px * (1 + fee))
                pos.invested += 1
                pos.peak = max(pos.peak, px)
            elif kind in ("reduce", "exit") and pos:
                sell = pos.shares / 2 if kind == "reduce" else pos.shares
                pos.proceeds += sell * px * (1 - fee - tax)
                pos.shares -= sell
                if kind == "reduce":
                    pos.reduced = True
                else:
                    trades.append(_close(pos, d, pending[1]))
                    pos = None
            pending = None
        tags = universe.get(d, {}).get(code)
        if pos is None and not tags:
            continue
        rec = rec_cache.get((code, d))
        if rec is None:
            rec = rec_cache[(code, d)] = hist.rec(code, d, P)
        if not rec or rec.get("close") is None:
            continue
        stock: dict = {"code": code}
        r2 = rec
        if pos:
            pos.peak = max(pos.peak, bar["close"])
            pos.low = min(pos.low, bar["low"] or bar["close"])
            f = hist.factor(code, d) or 1.0
            stock.update(holding=True, cost=pos.avg / f, buyDate=pos.entry_date)
            r2 = dict(rec, peakClose=pos.peak / f)
        sig = evaluate(r2, stock, P, S)
        light, rule = sig["light"], sig.get("rule", "")
        if events is not None:
            base = {"code": code, "date": d, "tags": sorted(tags or []), "regime": regimes[d]}
            if light == "go":
                events.append({**base, "kind": rule, **_f(hist.fwd(code, d))})
            elif light == "stop" and not pos:
                events.append({**base, "kind": "否決（未持有）", **_f(hist.fwd(code, d))})
            elif light == "wait" and rule == "大盤逆風":
                events.append({**base, "kind": "大盤逆風（被擋）", **_f(hist.fwd(code, d))})
            h = sig.get("hold")
            if h and h["light"] in ("exit", "reduce"):
                events.append({**base, "kind": ("退場：" if h["light"] == "exit" else "減碼：") + h["rule"], **_f(hist.fwd(code, d))})
        if pos is None:
            if light == "go" and tags:
                pending = ("buy", sorted(tags), rule)
        else:
            h = sig.get("hold") or {}
            if h.get("light") == "exit":
                pending = ("exit", h["rule"])
            elif h.get("light") == "reduce" and not pos.reduced:
                pending = ("reduce", h["rule"])
            elif light == "go" and rule == "加碼" and pos.adds < cfg.max_adds:
                pending = ("add",)
    if pos:  # 期末未平倉：以最後收盤計價
        last = max((d for d in dates if d in adj), default=None)
        if last:
            px = adj[last]["close"]
            pos.proceeds += pos.shares * px * (1 - fee - tax)
            pos.shares = 0
            trades.append(_close(pos, last, "期末未平倉"))
    for t in trades:
        t["code"] = code
    return trades


def _close(pos: Position, d: str, reason: str) -> dict:
    from .dates import to_date
    ret = (pos.proceeds / pos.invested - 1) * 100 if pos.invested else 0.0
    return {"entry": pos.entry_date, "exit": d, "days": (to_date(d) - to_date(pos.entry_date)).days,
            "tags": pos.tags, "rule": pos.entry_rule, "reason": reason, "invested": pos.invested,
            "pnl": pos.proceeds - pos.invested, "ret": round(ret, 3), "adds": pos.adds, "reduced": pos.reduced,
            "mae": round((pos.low / pos.avg - 1) * 100, 2) if pos.low < math.inf and pos.avg else None}


def _f(fw: dict) -> dict:
    return {f"f{h}": round(v, 3) for h, v in fw.items()}


# ---------------- 統計 ----------------

def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 2), "median": round(statistics.median(xs), 2),
            "win": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1)}


def half_year(d: str) -> str:
    return f"{d[:4]}{'上' if int(d[4:6]) <= 6 else '下'}"


def summarize_events(events: list[dict], baseline: dict, split: str) -> list[dict]:
    """依（種類 × 標籤 × 環境 × 樣本）分組：筆數、平均／中位數報酬、勝率、相對基準的超額報酬。"""
    groups: dict[tuple, list[dict]] = {}
    for e in events:
        sample = "樣本外" if e["date"] >= split else "樣本內"
        for tag in ["全部"] + e["tags"]:
            for reg in ("全部", e["regime"]):
                groups.setdefault((e["kind"], tag, reg, sample), []).append(e)
                groups.setdefault((e["kind"], tag, reg, "全期"), []).append(e)
    out = []
    for (kind, tag, reg, sample), es in sorted(groups.items()):
        row = {"kind": kind, "tag": tag, "regime": reg, "sample": sample}
        for h in HORIZONS:
            s = _stats([e[f"f{h}"] for e in es if f"f{h}" in e])
            row[f"f{h}"] = s
            b = baseline.get((tag, reg, sample), {}).get(h)
            if s["n"] and b and b["n"]:
                row[f"f{h}"]["excess"] = round(s["mean"] - b["mean"], 2)
        out.append(row)
    return out


def summarize_trades(trades: list[dict], split: str) -> dict:
    def block(ts):
        if not ts:
            return {"n": 0}
        rets = [t["ret"] for t in ts]
        wins = [t["pnl"] for t in ts if t["pnl"] > 0]
        loss = [-t["pnl"] for t in ts if t["pnl"] < 0]
        eq, peak, mdd = 0.0, 0.0, 0.0  # 依出場日累計損益（單位：每筆投入 1 單位資金）
        for t in sorted(ts, key=lambda t: t["exit"]):
            eq += t["pnl"]
            peak = max(peak, eq)
            mdd = max(mdd, peak - eq)
        return {"n": len(ts), "win": round(len(wins) / len(ts) * 100, 1), "meanRet": round(statistics.fmean(rets), 2),
                "medianRet": round(statistics.median(rets), 2), "pnl": round(sum(t["pnl"] for t in ts), 3),
                "profitFactor": round(sum(wins) / sum(loss), 2) if loss else None, "maxDD": round(mdd, 3),
                "avgDays": round(statistics.fmean(t["days"] for t in ts), 1),
                "worstMae": min((t["mae"] for t in ts if t["mae"] is not None), default=None)}
    return {"全期": block(trades), "樣本內": block([t for t in trades if t["entry"] < split]),
            "樣本外": block([t for t in trades if t["entry"] >= split])}


# ---------------- 主流程 ----------------

def run(db: DB, settings: dict, cfg: BTConfig, progress=None) -> dict:
    P0 = {**DEFAULT_RULES, **{p["id"]: p["value"] for p in settings["params"]}}
    for name, over in cfg.variants:
        bad = [k for k in over if k in INDICATOR_PARAMS]
        if bad:
            raise ValueError(f"參數組合不能包含 {', '.join(bad)}（會改變指標計算）")
        unknown = [k for k in over if k not in P0]
        if unknown:
            raise ValueError(f"未知的參數：{', '.join(unknown)}")
    all_dates = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]
    dates = [d for d in all_dates if cfg.start <= d <= cfg.end]
    if len(dates) < 20:
        raise ValueError(f"{cfg.start}～{cfg.end} 只有 {len(dates)} 個交易日的資料，請先執行 twradar backfill")
    warm = all_dates.index(dates[0])
    universe = build_universe(db, dates, cfg, settings["watchlist"])
    codes = sorted({c for u in universe.values() for c in u})
    hist = History(db, codes, all_dates[-1])
    regimes = {d: hist.regime(d) for d in dates}
    coverage = {"tradingDays": len(dates), "warmupDays": warm, "stocks": len(codes),
                "holdersWeeks": db.one("SELECT COUNT(DISTINCT date) AS n FROM holders WHERE date<=?", (cfg.end,))["n"],
                "regimeDays": {k: sum(1 for v in regimes.values() if v == k) for k in ("多頭", "空頭", "盤整", "未知")}}

    # 基準：股票池內每一天的未來報酬
    base_raw: dict[tuple, dict[int, list]] = {}
    for d in dates:
        reg = regimes[d]
        sample = "樣本外" if d >= cfg.split else "樣本內"
        for code, tags in universe[d].items():
            fw = hist.fwd(code, d)
            for tag in ["全部"] + sorted(tags):
                for r in ("全部", reg):
                    for s in (sample, "全期"):
                        b = base_raw.setdefault((tag, r, s), {h: [] for h in HORIZONS})
                        for h, v in fw.items():
                            b[h].append(v)
    baseline = {k: {h: _stats(v) for h, v in hs.items()} for k, hs in base_raw.items()}

    rec_cache: dict = {}
    results = []
    for vi, (name, over) in enumerate(cfg.variants):
        P = {**P0, **over}
        S = copy.deepcopy(settings)
        events: list | None = [] if vi == 0 else None   # 事件研究只對目前設定做
        trades: list[dict] = []
        for ci, code in enumerate(codes):
            trades += simulate(hist, code, dates, universe, P, S, cfg, rec_cache, events, regimes)
            if progress:
                progress(vi, len(cfg.variants), ci + 1, len(codes))
        res = {"name": name, "params": over, "trades": summarize_trades(trades, cfg.split)}
        if events is not None:
            res["events"] = summarize_events(events, baseline, cfg.split)
            res["eventCount"] = len(events)
            res["tradeList"] = trades
            res["byHalfYear"] = {hy: summarize_trades([t for t in trades if half_year(t["entry"]) == hy], "99999999")["全期"]
                                 for hy in sorted({half_year(t["entry"]) for t in trades})}
            res["byTag"] = {tag: summarize_trades([t for t in trades if tag in t["tags"]], cfg.split)
                            for tag in sorted({x for t in trades for x in t["tags"]})}
            res["byReason"] = {r: summarize_trades([t for t in trades if t["reason"] == r], cfg.split)["全期"]
                               for r in sorted({t["reason"] for t in trades})}
        results.append(res)
    bh = {}
    for tag in sorted({x for u in universe.values() for tags in u.values() for x in tags}):
        rets = []
        for code in codes:
            ds = [d for d in dates if code in universe[d] and tag in universe[d][code] and d in hist.adj[code]]
            if len(ds) >= 2:
                a, b = hist.adj[code][ds[0]]["close"], hist.adj[code][ds[-1]]["close"]
                if a and b:
                    rets.append((b / a - 1) * 100)
        bh[tag] = _stats(rets)
    return {"config": {"start": cfg.start, "end": cfg.end, "split": cfg.split, "universe": cfg.universe,
                       "maxAdds": cfg.max_adds, "horizons": list(HORIZONS)},
            "coverage": coverage, "baseline": {"|".join(k): v for k, v in baseline.items()},
            "buyHold": bh, "results": results}


def parse_grid(items: list[str]) -> list[tuple[str, dict]]:
    """["profitTargetPct=8,10,15", "stopLossPct=8,10"] → 笛卡兒積（最多 60 組），第一組永遠是目前設定。"""
    axes = []
    for it in items or []:
        k, _, vs = it.partition("=")
        vals = [float(v) if "." in v else int(v) for v in vs.split(",") if v.strip()]
        if not k or not vals:
            raise ValueError(f"參數組合格式錯誤：{it}（例：profitTargetPct=8,10,15）")
        axes.append([(k.strip(), v) for v in vals])
    combos = [dict(c) for c in itertools.product(*axes)] if axes else []
    if len(combos) > 60:
        raise ValueError(f"參數組合 {len(combos)} 組太多（上限 60）")
    return [("目前設定", {})] + [("、".join(f"{k}={v}" for k, v in c.items()), c) for c in combos]


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)
