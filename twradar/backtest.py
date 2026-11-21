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
from datetime import timedelta

from .analysis import DEFAULT_RULES, HOLDER_WEEKS, _holders, _indicators, adjust_prices, evaluate, whale_level
from .dates import to_date
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
    study: bool = False             # 條件研究（每個條件的區分力）
    scan: bool = False              # 全市場掃描的排序驗證


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
    def __init__(self, db: DB, codes: list[str], until: str, since: str = ""):
        """載入 codes 在 since～until（含）之間的資料；since 留空＝從頭載入。"""
        self.q: dict[str, list[dict]] = {c: [] for c in codes}
        self.ins: dict[str, list[dict]] = {c: [] for c in codes}
        self.mh: dict[str, list[dict]] = {c: [] for c in codes}
        ph = ",".join("?" * len(codes)) or "''"
        rng = (since, until)
        for r in db.q(f"SELECT code, date, open, high, low, close, change, volume, value, src FROM quotes "
                      f"WHERE code IN ({ph}) AND date>=? AND date<=? ORDER BY code, date", (*codes, *rng)):
            self.q[r["code"]].append(dict(r))
        for r in db.q(f"SELECT code, date, foreign_net, trust_net, dealer_net FROM inst WHERE code IN ({ph}) AND date>=? "
                      f"AND date<=? ORDER BY code, date", (*codes, *rng)):
            self.ins[r["code"]].append(dict(r))
        for r in db.q(f"SELECT code, date, margin_prev, margin_bal, short_prev, short_bal FROM margin WHERE code IN ({ph}) "
                      f"AND date>=? AND date<=? ORDER BY code, date", (*codes, *rng)):
            self.mh[r["code"]].append(dict(r))
        self.qd = {c: [r["date"] for r in v] for c, v in self.q.items()}
        self.raw = {c: {r["date"]: r for r in v} for c, v in self.q.items()}
        self.id_ = {c: [r["date"] for r in v] for c, v in self.ins.items()}
        self.md = {c: [r["date"] for r in v] for c, v in self.mh.items()}
        # 集保：每週一筆；回溯時只用「判定日之前」公布的週別（週五的資料週六才公布）
        self.hw: dict[tuple, dict] = {}
        self.hd: dict[str, list[str]] = {c: [] for c in codes}
        hsince = (to_date(since) - timedelta(days=70)).strftime("%Y%m%d") if since else ""
        for r in db.q(f"SELECT code, date, level, people, shares, pct FROM holders WHERE code IN ({ph}) AND date>=? AND date<=? "
                      f"ORDER BY code, date", (*codes, hsince, until)):
            w = self.hw.get((r["code"], r["date"]))
            if w is None:
                w = self.hw[(r["code"], r["date"])] = {}
                self.hd[r["code"]].append(r["date"])
            w[r["level"]] = {"date": r["date"], "people": r["people"], "shares": r["shares"], "pct": r["pct"]}
        self._hcache: dict[tuple, dict] = {}
        from .macro import Macro
        self.macro = Macro(db, until, (to_date(since) - timedelta(days=40)).strftime("%Y%m%d") if since else "")
        self.trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=? "
                                                "ORDER BY date", rng)]
        self.taiex = {r["date"]: r["taiex"] for r in db.q("SELECT date, taiex FROM market WHERE taiex IS NOT NULL "
                                                          "AND date>=? AND date<=?", rng)}
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
            rec.update(self.holders(code, d, P, q))
        return rec

    def holders(self, code: str, d: str, P: dict, q: list) -> dict:
        ds = self.hd.get(code) or []
        i = bisect.bisect_left(ds, d)          # 只取 d 之前的週別
        if i == 0:
            return {}
        closes = [r["close"] for r in q if r.get("close") is not None]
        avg = sum(closes) / len(closes) if closes else None
        lvl = whale_level(P, {}, avg)[0]
        key = (code, ds[i - 1], lvl)
        if key not in self._hcache:
            weeks = [self.hw[(code, x)] for x in reversed(ds[max(0, i - HOLDER_WEEKS):i])]
            self._hcache[key] = _holders(weeks, P, {}, avg)
        return self._hcache[key]

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
        out.update(self.macro.fields(d))
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
    events: list = field(default_factory=list)   # [{date, kind: 買進／加碼／減碼／賣出, rule, why}]
    score: float | None = None                    # 進場訊號當天的法人 5 日累計買超佔成交量 %（投資組合排序用）

    @property
    def avg(self) -> float:
        """平均買進成本（還原價，含手續費）。減碼不改變成本。"""
        return self.invested / self.bought if self.bought else 0.0


def simulate(hist: History, code: str, dates: list[str], universe: dict, P: dict, S: dict, cfg: BTConfig,
             rec_cache: dict, events: list | None, regimes: dict, daily: list | None = None) -> list[dict]:
    """daily：給個股頁用，記下每天的判定（燈號、規則、主要原因）。"""
    fee, tax = P.get("feePct", 0.1425) / 100, P.get("taxPct", 0.3) / 100
    trades: list[dict] = []
    pos: Position | None = None
    pending: tuple | None = None
    sig_day = ""            # 訊號出現的日子（隔天開盤才執行）
    adj = hist.adj[code]
    for d in dates:
        bar = adj.get(d)
        if bar is None or bar.get("close") is None:
            continue  # 沒有資料或當日無成交（暫停交易、處置停牌）：訊號留到下一個有成交的日子執行
        # 執行前一日的訊號（開盤價）
        if pending:
            px = bar["open"] or bar["close"]
            kind = pending[0]
            if kind in ("buy", "add"):
                if kind == "buy":
                    pos = Position(d, pending[1], entry_rule=pending[2], score=pending[4])
                    pos.events.append({"date": d, "kind": "買進", "rule": pending[2], "why": pending[3], "signal": sig_day})
                else:
                    pos.adds += 1
                    pos.events.append({"date": d, "kind": "加碼", "rule": "加碼", "why": pending[1], "signal": sig_day})
                pos.shares += 1 / (px * (1 + fee))
                pos.bought += 1 / (px * (1 + fee))
                pos.invested += 1
                pos.peak = max(pos.peak, px)
            elif kind in ("reduce", "exit") and pos:
                sell = pos.shares / 2 if kind == "reduce" else pos.shares
                pos.proceeds += sell * px * (1 - fee - tax)
                pos.shares -= sell
                pos.events.append({"date": d, "kind": "減碼" if kind == "reduce" else "賣出", "rule": pending[1], "why": pending[2], "signal": sig_day})
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
        if daily is not None:
            if pos:
                h = sig.get("hold") or {}
                daily.append({"date": d, "state": "hold-" + (h.get("light") or "na"), "rule": h.get("rule") or "續抱",
                              "note": "、".join((h.get("reasons") or [])[:3])})
            else:
                why = (sig.get("entryReasons") if light == "go" else sig.get("warnings") if light == "stop"
                       else sig.get("needs") if light == "wait" else sig.get("missing")) or []
                why = [x for x in dict.fromkeys(why) if x not in ("未持有", "持有中")]
                daily.append({"date": d, "state": light, "rule": rule, "note": "、".join(str(x) for x in why[:3])})
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
        sig_day = d
        if pos is None:
            if light == "go" and tags:
                f5, t5, v5 = r2.get("foreign5d"), r2.get("trust5d"), r2.get("vol5")
                score = ((f5 or 0) + (t5 or 0)) / (v5 * 5) * 100 if v5 and (f5 is not None or t5 is not None) else None
                pending = ("buy", sorted(tags), rule, sig.get("entryReasons") or [], score)
        else:
            h = sig.get("hold") or {}
            if h.get("light") == "exit":
                pending = ("exit", h["rule"], h.get("reasons") or [])
            elif h.get("light") == "reduce" and not pos.reduced:
                pending = ("reduce", h["rule"], h.get("reasons") or [])
            elif light == "go" and rule == "加碼" and pos.adds < cfg.max_adds:
                pending = ("add", sig.get("entryReasons") or [])
    if pos:  # 期末未平倉：以最後收盤計價
        last = max((d for d in dates if adj.get(d) and adj[d].get("close") is not None), default=None)
        if last:
            px = adj[last]["close"]
            pos.proceeds += pos.shares * px * (1 - fee - tax)
            pos.shares = 0
            trades.append(_close(pos, last, "期末未平倉"))
    for t in trades:
        t["code"] = code
    return trades


SLOTS = (5, 10, 20, 30, 50)   # 投資組合模擬：資金分成幾份
RANKED = (20, 30, 50)         # 另外試「同一天訊號太多時，挑法人買超最強的」


def _curve_stats(curve: list[tuple[str, float]], split: str) -> dict:
    """總報酬、年化、最大回落（逐日市值）、樣本內／外各自的報酬。"""
    from .dates import to_date
    if len(curve) < 2:
        return {}
    v0, v1 = curve[0][1], curve[-1][1]
    yrs = max((to_date(curve[-1][0]) - to_date(curve[0][0])).days / 365.25, 1e-9)
    peak, dd = -math.inf, 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    at = next((v for d, v in curve if d >= split), None)
    before = [v for d, v in curve if d < split]
    return {"total": round((v1 / v0 - 1) * 100, 2), "cagr": round(((v1 / v0) ** (1 / yrs) - 1) * 100, 2),
            "maxDD": round(dd * 100, 2),
            "in": round((before[-1] / v0 - 1) * 100, 2) if before else None,
            "out": round((v1 / before[-1] - 1) * 100, 2) if before and at is not None else None}


def portfolio(trades: list[dict], hist: History, dates: list[str], split: str, slots: int, fee: float, tax: float,
              rank: bool = False) -> dict:
    """把交易放進一個真實的投資組合：資金分成 slots 份，訊號來時有空位才買（同一天多檔時依代號順序），
    每次買進用「當時總資產 ÷ slots」；開盤成交、逐日以收盤計價，沒用到的錢放現金（不計利息）。
    簡化：不含加碼與減碼（每筆一次買進、一次賣出）。"""
    by_in: dict[str, list] = {}
    by_out: dict[str, list] = {}
    key = (lambda t: (t["entry"], -(t.get("score") if t.get("score") is not None else -1e9), t["code"])) if rank \
        else (lambda t: (t["entry"], t["code"]))
    for k, t in enumerate(sorted(trades, key=key)):
        by_in.setdefault(t["entry"], []).append((k, t))
        if t["reason"] != "期末未平倉":
            by_out.setdefault(t["exit"], []).append(k)
    cash, pos, last = 1.0, {}, {}
    curve, taken, skipped, expo = [], 0, 0, []

    def px(code, d, key):
        b = hist.adj.get(code, {}).get(d)
        v = b and (b.get(key) or b.get("close"))
        if v:
            last[code] = b.get("close") or v
        return v or last.get(code)
    for d in dates:
        for k in by_out.get(d, []):
            if k in pos:
                code, sh = pos.pop(k)
                p = px(code, d, "open")
                cash += sh * p * (1 - fee - tax) if p else 0
        for k, t in by_in.get(d, []):
            if len(pos) >= slots:
                skipped += 1
                continue
            p = px(t["code"], d, "open")
            if not p:
                continue
            eq = cash + sum(sh * (last.get(c) or 0) for c, sh in pos.values())
            alloc = min(cash, eq / slots)
            if alloc <= 0:
                continue
            pos[k] = (t["code"], alloc / (p * (1 + fee)))
            cash -= alloc
            taken += 1
        held = sum(sh * (px(c, d, "close") or 0) for c, sh in pos.values())
        curve.append((d, cash + held))
        expo.append(held / (cash + held) if cash + held else 0)
    out = _curve_stats(curve, split)
    out.update(slots=slots, taken=taken, skipped=skipped, exposure=round(sum(expo) / len(expo) * 100, 1) if expo else None,
               curve=_sample(curve), full=curve)
    return out


def _sample(curve: list) -> list:
    return [[d, round(v, 4)] for d, v in curve[::5]] + ([[curve[-1][0], round(curve[-1][1], 4)]] if curve else [])


def timed_0050(db: DB, hist: History, dates: list[str], split: str, P: dict, out_when) -> dict:
    """平常抱 0050（還原股息），out_when(當日大盤欄位, 大盤環境) 成立時隔日開盤賣出換現金，不成立時隔日開盤買回。
    ETF 證交稅 0.1%、手續費 0.1425%。"""
    h = History(db, ["0050"], dates[-1])
    bars = h.adj["0050"]
    fee, tax = P.get("feePct", 0.1425) / 100, 0.001
    cash, sh, want, curve, switches, last, days_in = 1.0, 0.0, True, [], 0, None, 0
    for d in dates:
        b = bars.get(d)
        if b and b.get("close"):
            px = b.get("open") or b["close"]
            if want and sh == 0 and cash > 0:
                sh, cash = cash / (px * (1 + fee)), 0.0
                switches += 1
            elif not want and sh > 0:
                cash, sh = sh * px * (1 - fee - tax), 0.0
                switches += 1
            last = b["close"]
        if last is None:
            continue
        curve.append((d, cash + sh * last))
        days_in += sh > 0
        want = not out_when(hist.market(d), hist.regime(d))
    o = _curve_stats(curve, split)
    o.update(switches=switches, exposure=round(days_in / len(curve) * 100, 1) if curve else None, curve=_sample(curve), full=curve)
    return o


def mix(a: list, b: list, w: float, split: str) -> dict:
    """兩條逐日曲線（起點都是 1）按 w : 1−w 一開始分配、之後不再平衡。"""
    bm = dict(b)
    curve = [(d, w * v / a[0][1] + (1 - w) * bm[d] / b[0][1]) for d, v in a if d in bm]
    o = _curve_stats(curve, split)
    o["curve"] = _sample(curve)
    return o


def benchmarks(db: DB, hist: History, dates: list[str], split: str) -> dict:
    """對照：加權指數（價格指數，不含股息）、0050（還原股息，含 0.1425% 買進手續費）。"""
    out = {}
    tx = [(d, hist.taiex.get(d)) for d in dates if hist.taiex.get(d)]
    if len(tx) >= 2:
        out["加權指數"] = {**_curve_stats(tx, split), "curve": [[d, round(v / tx[0][1], 4)] for d, v in tx[::5] + [tx[-1]]]}
    h = History(db, ["0050"], dates[-1])
    e = [(d, h.adj["0050"][d]["close"]) for d in dates if h.adj["0050"].get(d) and h.adj["0050"][d].get("close")]
    if len(e) >= 2:
        out["0050（含息）"] = {**_curve_stats(e, split), "curve": [[d, round(v / e[0][1], 4)] for d, v in e[::5] + [e[-1]]]}
    return out


def equity_curve(trades: list[dict]) -> list[list]:
    """累積損益（依出場日加總；每次買進投入 1 單位）：[[日期, 累積損益], ...]。"""
    by: dict[str, float] = {}
    for t in trades:
        by[t["exit"]] = by.get(t["exit"], 0.0) + t["pnl"]
    out, acc = [], 0.0
    for d in sorted(by):
        acc += by[d]
        out.append([d, round(acc, 3)])
    return out


def _close(pos: Position, d: str, reason: str) -> dict:
    from .dates import to_date
    ret = (pos.proceeds / pos.invested - 1) * 100 if pos.invested else 0.0
    if reason == "停損" and ret >= 0:
        reason = "移動停利"   # 停損類規則（跌破月線、獲利回吐…）觸發，但整筆是賺的
    return {"entry": pos.entry_date, "exit": d, "days": (to_date(d) - to_date(pos.entry_date)).days,
            "tags": pos.tags, "rule": pos.entry_rule, "reason": reason, "invested": pos.invested,
            "pnl": pos.proceeds - pos.invested, "ret": round(ret, 3), "adds": pos.adds, "reduced": pos.reduced,
            "mae": round((pos.low / pos.avg - 1) * 100, 2) if pos.low < math.inf and pos.avg else None,
            "events": pos.events, "score": None if pos.score is None else round(pos.score, 2)}


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
    from .conditions import BUILTIN as _CONDS
    for name, over in cfg.variants:
        for cid in [*(over.get("_drop") or []), *(over.get("_replace") or {}).values()]:
            if cid not in _CONDS and cid not in {c["id"] for c in settings.get("conditions", [])}:
                raise ValueError(f"未知的條件：{cid}")
        over = {k: v for k, v in over.items() if not k.startswith("_")}
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
        S, pover = apply_variant(settings, over)
        P = {**P0, **pover}
        events: list | None = [] if vi == 0 else None   # 事件研究只對目前設定做
        trades: list[dict] = []
        for ci, code in enumerate(codes):
            trades += simulate(hist, code, dates, universe, P, S, cfg, rec_cache, events, regimes)
            if progress:
                progress(vi, len(cfg.variants), ci + 1, len(codes))
        res = {"name": name, "params": over, "trades": summarize_trades(trades, cfg.split), "equity": equity_curve(trades),
               "byMonth": {m: summarize_trades([t for t in trades if t["entry"][:6] == m], "99999999")["全期"]
                           for m in sorted({t["entry"][:6] for t in trades})},
               "byTag": {tag: summarize_trades([t for t in trades if tag in t["tags"]], cfg.split)
                         for tag in sorted({x for t in trades for x in t["tags"]})}}
        fee_, tax_ = P.get("feePct", 0.1425) / 100, P.get("taxPct", 0.3) / 100
        pf = {str(n): portfolio(trades, hist, dates, cfg.split, n, fee_, tax_) for n in SLOTS}
        pf.update({f"{n}排序": portfolio(trades, hist, dates, cfg.split, n, fee_, tax_, rank=True) for n in RANKED})
        if vi == 0:
            full0 = {k: v.pop("full") for k, v in pf.items()}
            res["portfolio"] = pf
        else:     # 對照組只留 10 份與 30 份排序的數字，不留曲線
            res["portfolio"] = {k: {x: y for x, y in pf[k].items() if x not in ("curve", "full")} for k in ("10", "30排序")}
        if vi == 0 and cfg.study:
            from .study import condition_study
            res["conditions"] = condition_study(hist, dates, universe, P, S, cfg.split, rec_cache, regimes)
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
    scan_res = None
    if cfg.scan:
        from .scan import study as scan_study
        scan_res = scan_study(db, dates, cfg.split, progress=(lambda i, n: progress(len(cfg.variants), len(cfg.variants) + 1, i, n))
                              if progress else None)
    bench = benchmarks(db, hist, dates, cfg.split)
    # 抱 0050、只用大盤條件決定進出
    from .macro import foreign_signals
    P1 = {**P0, **{k: v for k, v in (cfg.variants[0][1] or {}).items() if not k.startswith("_")}}

    def fexit(n):
        return lambda m, reg: (lambda h: h[1] >= n and h[0] >= n)(foreign_signals(m, P1))
    timing_rules = {
        "0050＋外資撤出（3 項）時空手": fexit(3),
        "0050＋外資撤出（2 項）時空手": fexit(2),
        "0050＋大盤非多頭時空手": lambda m, reg: reg in ("盤整", "空頭"),
        "0050＋大盤空頭時空手": lambda m, reg: reg == "空頭",
        "0050＋外資撤出（2 項）且大盤非多頭時空手": lambda m, reg: fexit(2)(m, reg) and reg in ("盤整", "空頭"),
    }
    timing = {k: timed_0050(db, hist, dates, cfg.split, P1, f) for k, f in timing_rules.items()}
    combo = {}
    h50 = History(db, ["0050"], dates[-1]).adj["0050"]
    hold = [(d, h50[d]["close"]) for d in dates if h50.get(d) and h50[d].get("close")]
    sig = full0.get("30排序") if results else None
    if hold and sig:
        combo["一半抱 0050、一半照訊號（30 份排序）"] = mix(hold, sig, .5, cfg.split)
        t3 = timing["0050＋外資撤出（3 項）時空手"].get("full")
        if t3:
            combo["一半 0050＋外資撤出空手、一半照訊號"] = mix(t3, sig, .5, cfg.split)
    for v in timing.values():
        v.pop("full", None)
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
            "buyHold": bh, "benchmarks": bench, "timing": timing, "combo": combo, "results": results, "scan": scan_res}


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


# ---------------- 規則對照 ----------------
# 對照組＝（名稱, 覆寫）。覆寫除了參數，還可以有：
#   "_drop": [條件 id...]       從規則中拿掉這些條件（any／all 清單裡的項目；整條規則只剩它時，整條規則拿掉）
#   "_replace": {舊 id: 新 id}  把條件換成另一個（例如單日法人 → 5 日累計）
VETO_RULE = "否決"


def _veto_ids(settings: dict) -> list[str]:
    for r in settings["playbooks"]["entry"]:
        if r.get("label") == VETO_RULE and isinstance(r.get("when"), dict) and "any" in r["when"]:
            return [x for x in r["when"]["any"] if isinstance(x, str)]
    return []


def preset_variants(names: list[str], settings: dict) -> list[tuple[str, dict]]:
    """內建的規則對照組：
    largecap 大型權值股：停損放寬（3%、5%、不因法人賣超、連 2 日）、不追高（距月線 4%、6%）、盤整不進場（月線 10 日 1%、2%）
    climate-grid  v0.6 穩健性：v0.5、外資撤出 2 項、3 項但期貨空單 1000／4000 口、新台幣貶 0.2／0.5%
    v05     前一版規則
    climate 大盤環境（以 v0.5 為底）：市場廣度 30／40／50%、指數漲廣度跌、外資撤出（3 項、2 項）、外資撤出＋廣度
    tdcc    集保籌碼集中（大戶連升＋股東人數連降）：試單、試單須集中、上車須集中、上車須大戶連升
    settled 起漲前籌碼沉澱：試單、試單須沉澱、上車須沉澱
    trial   加「試單」規則（另一組同時拿掉大盤逆風）
    bullhold／bullstop／bull  大盤多頭時不因警訊減碼／放寬月線停損／兩者都做
    v04／v03  前幾版規則（整套換掉）
    veto    否決條件逐條拿掉（每條一組），再加「全部拿掉」
    inst1d  法人條件改回看單日
    market  拿掉「大盤逆風」"""
    out: list[tuple[str, dict]] = []
    for n in [x.strip() for x in names for x in x.split(",") if x.strip()]:
        if n == "veto":
            ids = _veto_ids(settings)
            out += [(f"不用否決：{i}", {"_drop": [i]}) for i in ids]
            if ids:
                out.append(("不用任何否決", {"_drop": ids}))
        elif n == "inst1d":
            out.append(("法人看單日", {"_replace": {"inst5dBuy": "instBuy", "trustBuyConflict5d": "trustBuyConflict"}}))
        elif n == "v04":
            from .rules import v04_playbooks
            out.append(("v0.4（前一版）", {"_playbooks": v04_playbooks()}))
        elif n == "v03":
            from .rules import v03_playbooks
            out.append(("v0.3", {"_playbooks": v03_playbooks()}))
        elif n == "trial":
            from .rules import trial_playbooks
            out.append(("加試單", {"_playbooks": trial_playbooks()}))
            out.append(("加試單＋不用大盤逆風", {"_playbooks": trial_playbooks(), "_drop": ["marketWeak"]}))
        elif n == "settled":
            from .rules import settled_playbooks, trial_playbooks
            out.append(("加試單", {"_playbooks": trial_playbooks()}))
            out.append(("加試單（須籌碼沉澱）", {"_playbooks": trial_playbooks(settled=True)}))
            out.append(("上車須籌碼沉澱", {"_playbooks": settled_playbooks()}))
        elif n == "tdcc":
            from .rules import settled_playbooks, trial_playbooks, trial_with
            out.append(("加試單", {"_playbooks": trial_playbooks()}))
            out.append(("加試單（須籌碼集中）", {"_playbooks": trial_with("chipsGathering")}))
            out.append(("上車須籌碼集中", {"_playbooks": settled_playbooks("chipsGathering")}))
            out.append(("上車須大戶連升（不看股東人數）", {"_playbooks": settled_playbooks("chipsGathering"), "holdersDownWeeksMin": 0}))
        elif n in ("bullhold", "bullstop", "bull"):
            from .rules import bull_playbooks
            name = {"bullhold": "多頭不因警訊減碼", "bullstop": "多頭放寬月線停損", "bull": "多頭不減碼＋放寬停損"}[n]
            out.append((name, {"_playbooks": bull_playbooks(hold=n != "bullstop", stop=n != "bullhold")}))
        elif n == "bullstop-grid":   # 穩健性：放寬幅度換成相鄰數值，以及只放寬幅度（保留「跌破月線＋法人賣超」停損）
            from .rules import bull_playbooks, v04_playbooks
            for v in (2, 2.5, 3, 4, 5):
                out.append((f"多頭放寬月線停損 {v}%", {"_playbooks": bull_playbooks(stop=True), "breakMaBullPct": v}))
            pb = bull_playbooks(stop=True)
            base = next(r for r in v04_playbooks()["hold"] if r.get("label") == "停損")["when"]["any"]
            for r in pb["hold"]:
                if r.get("label") == "停損":
                    r["when"]["any"] = [x if not (isinstance(x, dict) and "instNetSell" in (x.get("all") or [])) else
                                        next(y for y in base if isinstance(y, dict) and "instNetSell" in (y.get("all") or []))
                                        for x in r["when"]["any"]]
            out.append(("多頭只放寬幅度 3%（法人賣超停損照舊）", {"_playbooks": pb}))
        elif n == "climate":   # 大盤環境：市場廣度、外資態度（現貨＋期貨＋匯率）
            from .rules import veto_with
            for v in (30, 40, 50):
                out.append((f"廣度 < {v}% 不上車", {"_playbooks": veto_with("breadthWeak"), "breadthMinPct": v}))
            out.append(("指數漲、廣度跌不上車", {"_playbooks": veto_with("breadthDiverge")}))
            out.append(("外資撤出（3 項）不上車", {"_playbooks": veto_with("foreignExit")}))
            out.append(("外資撤出（2 項）不上車", {"_playbooks": veto_with("foreignExit"), "foreignExitSignals": 2}))
            out.append(("外資撤出＋廣度 < 40% 不上車", {"_playbooks": veto_with("foreignExit", "breadthWeak")}))
        elif n == "largecap":   # 大型權值股：停損放寬、不追高、盤整不進場（看報表的「依股票組」比較大型／中型／上櫃）
            from .rules import entry_with, stop_variant
            out.append(("停損：跌破月線 3% 才算", {"breakMaPct": 3}))
            out.append(("停損：跌破月線 5% 才算", {"breakMaPct": 5, "breakMaBullPct": 5}))
            out.append(("停損：不因月線＋法人賣超", {"_playbooks": stop_variant("nosell")}))
            out.append(("停損：連 2 日在月線下", {"_playbooks": stop_variant("two")}))
            out.append(("不追高：距月線 ≤ 4%", {"_playbooks": entry_with("notExtendedMa20"), "maxBiasMa20Pct": 4}))
            out.append(("不追高：距月線 ≤ 6%", {"_playbooks": entry_with("notExtendedMa20"), "maxBiasMa20Pct": 6}))
            out.append(("盤整不進場：月線 10 日 ≥ 1%", {"_playbooks": entry_with("ma20Trending"), "ma20SlopePct": 1}))
            out.append(("盤整不進場：月線 10 日 ≥ 2%", {"_playbooks": entry_with("ma20Trending"), "ma20SlopePct": 2}))
        elif n == "v05":
            from .rules import v05_playbooks
            out.append(("v0.5（前一版）", {"_playbooks": v05_playbooks()}))
        elif n == "climate-grid":   # v0.6 穩健性：外資撤出的門檻換成相鄰數值
            from .rules import v05_playbooks
            out.append(("v0.5（不看外資）", {"_playbooks": v05_playbooks()}))
            out.append(("外資撤出 2 項", {"foreignExitSignals": 2}))
            for lots, fx in ((1000, 0.3), (4000, 0.3), (2000, 0.2), (2000, 0.5), (1000, 0.2), (4000, 0.5)):
                out.append((f"3 項：空單 {lots} 口、貶 {fx}%", {"foreignFutChgLots": lots, "foreignFxPct": fx}))
        elif n == "market":
            out.append(("不用大盤逆風", {"_drop": ["marketWeak"]}))
        else:
            raise ValueError(f"未知的對照組：{n}（可用：largecap、climate、climate-grid、v05、tdcc、settled、trial、bullhold、bullstop、bullstop-grid、bull、veto、inst1d、market、v04、v03）")
    return out


def _transform(expr, drop: set, rep: dict):
    """回傳轉換後的規則運算式；整個運算式被拿掉時回傳 None。"""
    if isinstance(expr, str):
        return None if expr in drop else rep.get(expr, expr)
    if isinstance(expr, list):
        xs = [y for y in (_transform(x, drop, rep) for x in expr) if y is not None]
        return xs or None
    if isinstance(expr, dict):
        out = dict(expr)
        for k in ("all", "any", "none"):
            if k in out:
                xs = _transform(out[k], drop, rep)
                if xs is None:
                    return None
                out[k] = xs
        if "not" in out:
            x = _transform(out["not"], drop, rep)
            if x is None:
                return None
            out["not"] = x
        if "atLeast" in out:
            of = _transform(out["atLeast"]["of"], drop, rep)
            if of is None:
                return None
            out["atLeast"] = {**out["atLeast"], "of": of, "n": min(out["atLeast"]["n"], len(of))}
        return out
    return expr


def apply_variant(settings: dict, over: dict) -> tuple[dict, dict]:
    """（設定, 參數覆寫）：把 _drop／_replace 套到規則上。"""
    drop, rep = set(over.get("_drop") or []), dict(over.get("_replace") or {})
    params = {k: v for k, v in over.items() if not k.startswith("_")}
    S = copy.deepcopy(settings)
    if over.get("_playbooks"):
        S["playbooks"] = copy.deepcopy(over["_playbooks"])
    if drop or rep:
        for side in ("entry", "hold"):
            rules = []
            for r in S["playbooks"].get(side) or []:
                if r.get("default"):
                    rules.append(r)
                    continue
                w = _transform(r["when"], drop, rep)
                if w is not None:
                    rules.append({**r, "when": w})
            S["playbooks"][side] = rules
    return S, params


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)
