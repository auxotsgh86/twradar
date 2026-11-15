"""條件研究：每個條件單獨看，成立與不成立時，之後的超額報酬差多少（區分力）。

- 股票池、報酬定義與回溯相同（隔日開盤買、持有 h 日後收盤、還原價）。
- 超額報酬＝個股報酬 − 同日同組別（大型／中型／上櫃／觀察清單）股票池平均。
- 同一天的觀察彼此相關：先算每天的平均，再算日平均的平均與標準誤（se）。
- 兩個母體：「全部」＝整個股票池；「趨勢股」＝收盤在月線上且月線走平或上揚（右側系統真正會考慮的股票）。
- 「差距」＝成立 − 不成立；否決類條件應該是負的，買進類條件應該是正的；|差距| < 2×se 代表看不出區分力。
"""
from __future__ import annotations

import math
import statistics

from .conditions import BUILTIN, NA, PASS, Ctx

HORIZONS = (5, 10, 20)
GROUPS = ("大型", "中型", "上櫃", "觀察清單")
# 需要持有成本、關鍵價的條件不研究（股票池一律未持有）
SKIP = {"noData", "isHolding", "notHolding", "addProfitOk", "stopLoss", "profitTarget", "belowKeyLevel", "giveback"}


def _grp(tags) -> str:
    return next((g for g in GROUPS if g in tags), "觀察清單")


def _agg(means: list[float], n: int) -> dict:
    if not means:
        return {"n": 0, "days": 0}
    out = {"n": n, "days": len(means), "excess": round(statistics.fmean(means), 2)}
    if len(means) >= 2:
        out["se"] = round(statistics.stdev(means) / math.sqrt(len(means)), 2)
    return out


def condition_study(hist, dates, universe, P, S, split, rec_cache=None, regimes=None) -> dict:
    """回傳 {條件: {"label", "side", 母體: {樣本: {"pass": {h: 統計}, "fail": {...}, "diff": {h: 差距}}}}}。"""
    rec_cache = {} if rec_cache is None else rec_cache
    conds = [c for cid, c in BUILTIN.items() if cid not in SKIP]
    # 每天每組的基準
    fw_all: dict[tuple, dict[int, float]] = {}
    base: dict[tuple, dict[int, float]] = {}
    for d in dates:
        acc: dict[str, dict[int, list]] = {}
        for code, tags in universe[d].items():
            fw = hist.fwd(code, d)
            fw_all[(code, d)] = fw
            for h, v in fw.items():
                acc.setdefault(_grp(tags), {}).setdefault(h, []).append(v)
        for g, hs in acc.items():
            base[(d, g)] = {h: statistics.fmean(v) for h, v in hs.items()}
    # 每天先累計（總和、筆數），一天結束時換成當天平均：(條件, 母體, 狀態) → [s5, s10, s20, n5, n10, n20]
    # daily[(條件, 母體, 樣本, 狀態)][h] = [當天平均...]；counts 為總筆數
    daily: dict[tuple, dict[int, list[float]]] = {}
    counts: dict[tuple, dict[int, int]] = {}
    hi = {h: k for k, h in enumerate(HORIZONS)}
    for d in dates:
        smp = "樣本外" if d >= split else "樣本內"
        day: dict[tuple, list[float]] = {}
        for code, tags in universe[d].items():
            fw = fw_all.get((code, d))
            if not fw:
                continue
            rec = rec_cache.get((code, d))
            if rec is None:
                rec = rec_cache[(code, d)] = hist.rec(code, d, P)
            if not rec or rec.get("close") is None:
                continue
            b = base.get((d, _grp(tags)), {})
            ex = [(hi[h], fw[h] - b[h]) for h in HORIZONS if h in fw and h in b]
            if not ex:
                continue
            ma20, prev = rec.get("ma20"), rec.get("ma20Prev")
            trend = ma20 is not None and prev is not None and rec["close"] >= ma20 and ma20 >= prev
            pops = ("全部", "趨勢股") if trend else ("全部",)
            ctx = Ctx(rec, {"code": code}, P)
            for c in conds:
                try:
                    st = c.fn(ctx).state
                except Exception:  # noqa: BLE001  條件缺參數或資料型別不符：當作資料不足
                    continue
                if st == NA:
                    continue
                state = "pass" if st == PASS else "fail"
                for pop in pops:
                    a = day.get((c.id, pop, state))
                    if a is None:
                        a = day[(c.id, pop, state)] = [0.0, 0.0, 0.0, 0, 0, 0]
                    for k, v in ex:
                        a[k] += v
                        a[k + 3] += 1
        for (cid, pop, state), a in day.items():
            for s in (smp, "全期"):
                dd = daily.setdefault((cid, pop, s, state), {h: [] for h in HORIZONS})
                cc = counts.setdefault((cid, pop, s, state), {h: 0 for h in HORIZONS})
                for h, k in hi.items():
                    if a[k + 3]:
                        dd[h].append(a[k] / a[k + 3])
                        cc[h] += a[k + 3]
    out: dict = {}
    for c in conds:
        row = {"label": c.label, "side": c.side}
        for pop in ("全部", "趨勢股"):
            for s in ("樣本內", "樣本外", "全期"):
                cell = {"pass": {}, "fail": {}, "diff": {}}
                for h in HORIZONS:
                    a = _agg(daily.get((c.id, pop, s, "pass"), {}).get(h, []), counts.get((c.id, pop, s, "pass"), {}).get(h, 0))
                    f = _agg(daily.get((c.id, pop, s, "fail"), {}).get(h, []), counts.get((c.id, pop, s, "fail"), {}).get(h, 0))
                    cell["pass"][f"f{h}"], cell["fail"][f"f{h}"] = a, f
                    if a.get("days") and f.get("days"):
                        cell["diff"][f"f{h}"] = round(a["excess"] - f["excess"], 2)
                row.setdefault(pop, {})[s] = cell
        cell = row["全部"]["全期"]
        if cell["pass"]["f10"].get("n") or cell["fail"]["f10"].get("n"):
            out[c.id] = row
    return out


def verdict(cell: dict, h: str = "f10") -> str:
    """一句話：成立時之後表現較好／較差／看不出差別。"""
    a, f = cell["pass"].get(h, {}), cell["fail"].get(h, {})
    if not a.get("n") or not f.get("n"):
        return "樣本不足"
    if a["n"] < 30 or a.get("days", 0) < 10:
        return "樣本不足"
    se = math.hypot(a.get("se") or 0, f.get("se") or 0)
    diff = a["excess"] - f["excess"]
    if se and abs(diff) < 2 * se:
        return "看不出差別"
    return "成立後較好" if diff > 0 else "成立後較差"
