"""大盤資金向量（淨方向＋分歧度）的量化驗證。獨立模組：不影響規則、條件或回溯預設。

特徵（判定日 d 只用 date ≤ d 的資料，正＝偏多、負＝偏空）：
- spot：外資現貨 5 日累計買賣超（億元）
- fut ：外資台指期淨未平倉 5 日變化（口；正＝空單減少）
- fx  ：−（美元兌新台幣 5 日變化 %）（正＝新台幣升值）
每項做滾動 Z-score（視窗 W 個交易日，含當天），截斷在 ±3。
- net_score  ：可用 Z-score 的等權平均（至少 2 項）
- dispersion ：可用 Z-score 的樣本標準差（至少 2 項；現貨、期貨、匯率互相打架時變大）
驗證：未來 h 個交易日的加權指數報酬、期間最大回落（以 d 收盤為基準、未來 h 日內最低收盤的跌幅，沒跌時為 0）。
"""
from __future__ import annotations

import math
from statistics import fmean, stdev

from .db import DB

ZCAP = 3.0
BUCKETS = (("net < −1", lambda v: v < -1.0), ("−1 ≤ net < 0", lambda v: -1.0 <= v < 0),
           ("0 ≤ net ≤ +1", lambda v: 0 <= v <= 1.0), ("net > +1", lambda v: v > 1.0))
HIGH_DISP = 1.2


# ---------------- 資料與特徵 ----------------

def load(db: DB, until: str | None = None) -> dict:
    """交易日序列（加權指數有值的日子）與對齊的原始欄位。until：只載入 date ≤ until（測試防未來函數用）。"""
    lim = until or "99999999"
    m = db.q("SELECT date, taiex, foreign_amt FROM market WHERE taiex IS NOT NULL AND date<=? ORDER BY date", (lim,))
    mac = {r["date"]: dict(r) for r in db.q("SELECT date, fut_foreign, usd_twd FROM macro WHERE date<=?", (lim,))}
    days = [r["date"] for r in m]
    return {"days": days, "taiex": [r["taiex"] for r in m], "spot": [r["foreign_amt"] for r in m],
            "fut": [(mac.get(d) or {}).get("fut_foreign") for d in days],
            "fx": [(mac.get(d) or {}).get("usd_twd") for d in days]}


def raw_features(s: dict) -> dict[str, list]:
    """每天的 3 個 5 日變化量（方向已統一：正＝偏多）。只用當天與之前的資料。"""
    n = len(s["days"])
    spot, fut, fx = [None] * n, [None] * n, [None] * n
    for i in range(n):
        w = s["spot"][i - 4:i + 1] if i >= 4 else []
        if len(w) == 5 and None not in w:
            spot[i] = sum(w) / 1e8
        if i >= 5 and s["fut"][i] is not None and s["fut"][i - 5] is not None:
            fut[i] = s["fut"][i] - s["fut"][i - 5]
        if i >= 5 and s["fx"][i] and s["fx"][i - 5]:
            fx[i] = -(s["fx"][i] / s["fx"][i - 5] - 1) * 100
    return {"spot": spot, "fut": fut, "fx": fx}


def rolling_z(xs: list, window: int, min_obs: int | None = None) -> list:
    """滾動 Z-score：今天的值相對於「含今天的最近 window 天」的平均與樣本標準差；截斷在 ±ZCAP。"""
    need = min_obs or max(20, int(window * .8))
    out = [None] * len(xs)
    for i, x in enumerate(xs):
        if x is None:
            continue
        w = [v for v in xs[max(0, i - window + 1):i + 1] if v is not None]
        if len(w) < need:
            continue
        sd = stdev(w)
        if sd == 0:
            continue
        out[i] = max(-ZCAP, min(ZCAP, (x - fmean(w)) / sd))
    return out


def vector(s: dict, window: int) -> dict[str, list]:
    raw = raw_features(s)
    z = {k: rolling_z(v, window) for k, v in raw.items()}
    n = len(s["days"])
    net, disp = [None] * n, [None] * n
    for i in range(n):
        zs = [z[k][i] for k in ("spot", "fut", "fx") if z[k][i] is not None]
        if len(zs) >= 2:
            net[i] = fmean(zs)
            disp[i] = stdev(zs)
    return {**{f"z_{k}": v for k, v in z.items()}, "net": net, "disp": disp, **{f"raw_{k}": v for k, v in raw.items()}}


def forward(taiex: list, h: int) -> tuple[list, list]:
    """未來 h 日報酬、期間最大回落（%）。最後 h 天沒有完整未來資料 → None。"""
    n = len(taiex)
    ret, mdd = [None] * n, [None] * n
    for i in range(n - h):
        c = taiex[i]
        if not c:
            continue
        ret[i] = (taiex[i + h] / c - 1) * 100
        mdd[i] = min(0.0, (min(taiex[i + 1:i + h + 1]) / c - 1) * 100)
    return ret, mdd


# ---------------- 統計 ----------------

def ranks(xs: list[float]) -> list[float]:
    """平均排名（同分取平均，從 1 開始）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs: list, ys: list) -> tuple[float | None, int]:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 10:
        return None, n
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = fmean(rx), fmean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return (None if not sx or not sy else sxy / (sx * sy)), n


def episodes(idx: list[int], gap: int = 10) -> int:
    """連續（相隔不超過 gap 個交易日）出現的日子算同一段。"""
    return 0 if not idx else 1 + sum(1 for a, b in zip(idx, idx[1:]) if b - a > gap)


def group(idx: list[int], ret: list, mdd: list) -> dict:
    ix = [i for i in idx if ret[i] is not None]
    if not ix:
        return {"n": 0, "episodes": 0}
    r, m = [ret[i] for i in ix], [mdd[i] for i in ix]
    return {"n": len(ix), "episodes": episodes(ix), "ret": round(fmean(r), 2), "up": round(sum(x > 0 for x in r) / len(r) * 100),
            "mdd": round(fmean(m), 2), "worst": round(min(m), 2)}


# ---------------- 主流程 ----------------

def study(db: DB, windows=(60,), horizons=(10, 20), split: str | None = None, start: str = "", P: dict | None = None) -> dict:
    from .analysis import DEFAULT_RULES
    from .macro import foreign_signals
    P = {**DEFAULT_RULES, **(P or {})}
    s = load(db)
    days, n = s["days"], len(s["days"])
    vecs = {w: vector(s, w) for w in windows}
    base = vecs[windows[0]]["net"]
    period = [i for i in range(n) if base[i] is not None and days[i] >= start]
    if len(period) < 30:
        return {"error": f"可用的日子太少（{len(period)} 天）：需要外資期貨與匯率資料（先跑 macro-history）"}
    split = split or days[period[len(period) * 2 // 3]]
    fwd = {h: forward(s["taiex"], h) for h in horizons}
    raw = vecs[windows[0]]
    fe = [None] * n   # 現行 foreignExit（三項門檻），用同一組原始特徵
    for i in range(n):
        rec = {"foreignSpot5d": raw["raw_spot"][i], "futForeignChg5": raw["raw_fut"][i],
               "usdTwdChg5Pct": None if raw["raw_fx"][i] is None else -raw["raw_fx"][i]}
        hit, have, _ = foreign_signals(rec, P)
        need = P.get("foreignExitSignals", 3)
        fe[i] = None if have < need else hit >= need
    out = {"start": days[period[0]], "end": days[period[-1]], "split": split, "windows": {}}
    samples = {"IS": [i for i in period if days[i] < split], "OOS": [i for i in period if days[i] >= split]}
    for w, v in vecs.items():
        net, disp = v["net"], v["disp"]
        res = {"ic": {}, "groups": {}, "foreignExit": {}}
        for sm, idx in samples.items():
            for h in horizons:
                ret, mdd = fwd[h]
                ic_r = spearman([net[i] for i in idx], [ret[i] for i in idx])
                ic_m = spearman([net[i] for i in idx], [mdd[i] for i in idx])
                res["ic"][f"{sm}:{h}"] = {"ret": ic_r[0], "mdd": ic_m[0], "n": ic_r[1]}
                g = {"全部": group([i for i in idx if net[i] is not None], ret, mdd)}
                for name, f in BUCKETS:
                    g[name] = group([i for i in idx if net[i] is not None and f(net[i])], ret, mdd)
                g[f"高內耗 disp ≥ {HIGH_DISP}"] = group([i for i in idx if disp[i] is not None and disp[i] >= HIGH_DISP], ret, mdd)
                res["groups"][f"{sm}:{h}"] = g
                res["foreignExit"][f"{sm}:{h}"] = {
                    "外資撤出成立（擋掉）": group([i for i in idx if fe[i] is True], ret, mdd),
                    "外資撤出不成立": group([i for i in idx if fe[i] is False], ret, mdd),
                    "net < −1（向量偏空）": g["net < −1"],
                }
        out["windows"][w] = res
    return out


def render(r: dict, horizons=(10, 20)) -> str:
    if "error" in r:
        return r["error"]
    L = [f"期間 {r['start']}～{r['end']}，樣本外從 {r['split']} 開始（前 2/3 樣本內）",
         "報酬與回落單位：%；回落＝未來 h 日內最低收盤相對判定日收盤（沒跌為 0）；段＝相隔 10 個交易日以內算同一段"]

    def f(x, d=2):
        return "—" if x is None else f"{x:+.{d}f}"
    for w, res in r["windows"].items():
        L.append(f"\n######## 滾動視窗 W = {w} ########")
        L.append("\n[1] Rank IC（Spearman：net_score 與未來報酬／回落；回落是負數，IC 為正代表偏多時跌得少）")
        L.append(f"{'樣本':<6}{'h':>4}{'IC(報酬)':>10}{'IC(回落)':>10}{'天數':>7}")
        for sm in ("IS", "OOS"):
            for h in horizons:
                x = res["ic"][f"{sm}:{h}"]
                L.append(f"{sm:<6}{h:>4}{f(x['ret'], 3):>10}{f(x['mdd'], 3):>10}{x['n']:>7}")
        for title, key in (("[2] net_score 分組與高內耗", "groups"), ("[3] 對照：現行外資撤出（3 項全成立）", "foreignExit")):
            L.append(f"\n{title}")
            for h in horizons:
                L.append(f"-- 未來 {h} 日")
                L.append(f"{'組別':<22}{'樣本':<5}{'天數':>6}{'段':>5}{'平均報酬':>9}{'上漲%':>7}{'平均回落':>9}{'最差回落':>9}")
                for sm in ("IS", "OOS"):
                    for name, g in res[key][f"{sm}:{h}"].items():
                        if not g["n"]:
                            L.append(f"{name:<22}{sm:<5}{0:>6}")
                            continue
                        L.append(f"{name:<22}{sm:<5}{g['n']:>6}{g['episodes']:>5}{f(g['ret']):>9}{g['up']:>6}%{f(g['mdd']):>9}{f(g['worst']):>9}")
    return "\n".join(L)
