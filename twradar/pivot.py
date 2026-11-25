"""「平點／轉折」研究：把價格切成 W 天一段算斜率，斜率由跌（漲）轉平時，是不是波谷（波峰）？獨立模組：不影響規則與回溯預設。

定義（第 i 天收盤判定，只用第 i 天以前的資料；還原股價）：
- 這段斜率 s_now = C[i]/C[i−W] − 1；前一段斜率 s_prev = C[i−W]/C[i−2W] − 1
- σ_W = 近 60 日日報酬標準差 × √W（各股自己的波動，讓高低波動股用同一把尺）
- 由跌轉平（谷）：s_prev ≤ −σ_W 且 |s_now| ≤ 0.5 σ_W；由漲轉平（峰）：s_prev ≥ σ_W 且 |s_now| ≤ 0.5 σ_W
- 月線向上：今天的 20 日均價 > 10 天前的 20 日均價
- 量縮／量增：近 W 日均量 ÷ 再之前 20 日均量 ≤ 0.8／≥ 1.3
- 法人買超：外資＋投信近 5 日合計 > 0
- 由跌轉漲（轉折↑）：s_prev ≤ −σ_W 且 s_now ≥ +0.5 σ_W；由漲轉跌（轉折↓）：鏡像
- 平點的轉換率：平點之後的下一段 W 天轉上（≥ +0.5σ）、續跌（≤ −0.5σ）或盤整；並比較有／沒有某個特徵時的轉上率
  （特徵都在平點當天、只用當天以前的資料）
- 只算「第一天」：條件今天成立、昨天不成立（連續好幾天只算一次）
驗證：隔天開盤買進、持有到第 i+h 天收盤的報酬；和同一段期間（樣本內／外）所有股票日子的平均比較（超額）。
股票池：每季依前 60 日成交金額排名的大型、中型、上櫃（與回溯相同、時點正確）。
"""
from __future__ import annotations

import math
from statistics import fmean, stdev

from .backtest import BTConfig, History, rank_universe
from .db import DB

FLAT, MOVE, QUIET, LOUD = 0.5, 1.0, 0.8, 1.3
GROUPS = (
    ("全部日子（基準）", "base"),
    ("月線向上的日子", "up"),
    ("谷：由跌轉平", "trough"),
    ("谷＋月線向上", "trough_up"),
    ("谷＋月線向上＋回檔量縮", "trough_up_quiet"),
    ("谷＋月線向上＋量縮＋法人買超", "trough_up_quiet_inst"),
    ("峰：由漲轉平", "peak"),
    ("峰＋量增", "peak_loud"),
    ("峰＋量增＋月線向上", "peak_loud_up"),
    ("轉折↑：由跌轉漲", "turn_up"),
    ("轉折↑＋月線向上", "turn_up_up"),
    ("轉折↑＋量增", "turn_up_loud"),
    ("轉折↓：由漲轉跌", "turn_dn"),
    ("轉折↓＋量增", "turn_dn_loud"),
)
TURN = 0.5          # 轉折：這一段斜率 ≥ +0.5σ（由跌轉漲）或 ≤ −0.5σ（由漲轉跌）
# 平點之後的下一段（W 天）：轉上（≥ +0.5σ）、續跌（≤ −0.5σ）、盤整；以及哪些特徵讓平點比較會轉上
FEATURES = (
    ("月線向上", "up"),
    ("回檔量縮", "quiet"),
    ("法人 5 日買超", "inst"),
    ("法人連買 ≥ 2 天", "streak2"),
    ("前一段跌很深（≤ −2σ）", "deep"),
    ("平點當天收紅", "red"),
)


def log_returns(C: list) -> list:
    return [None] + [math.log(C[k] / C[k - 1]) if C[k] and C[k - 1] and C[k] > 0 and C[k - 1] > 0 else None
                     for k in range(1, len(C))]


def flags(C: list, V: list, N: list, i: int, W: int, lr: list | None = None) -> dict | None:
    """第 i 天的狀態（只用 0..i）。C 收盤（還原）、V 成交量、N 法人（外資＋投信）買賣超，缺值為 None；lr＝log_returns(C)。"""
    if i < max(2 * W, 61) or None in C[i - max(2 * W, 61):i + 1]:
        return None
    lr = lr or log_returns(C)
    rets = [x for x in lr[i - 59:i + 1] if x is not None]
    if len(rets) < 40:
        return None
    sig = stdev(rets) * math.sqrt(W)
    if sig <= 0:
        return None
    now, prev = C[i] / C[i - W] - 1, C[i - W] / C[i - 2 * W] - 1
    flat = abs(now) <= FLAT * sig
    up = fmean(C[i - 19:i + 1]) > fmean(C[i - 29:i - 9])
    v1, v0 = V[i - W + 1:i + 1], V[i - W - 19:i - W + 1]
    vr = fmean(v1) / fmean(v0) if None not in v1 and None not in v0 and len(v0) == 20 and fmean(v0) > 0 else None
    n5 = N[i - 4:i + 1]
    inst = sum(n5) > 0 if None not in n5 else None
    trough, peak = flat and prev <= -MOVE * sig, flat and prev >= MOVE * sig
    tu, td = prev <= -MOVE * sig and now >= TURN * sig, prev >= MOVE * sig and now <= -TURN * sig
    quiet, loud = vr is not None and vr <= QUIET, vr is not None and vr >= LOUD
    streak2 = None if None in N[i - 1:i + 1] else N[i] > 0 and N[i - 1] > 0
    return {"base": True, "up": up, "trough": trough, "trough_up": trough and up,
            "trough_up_quiet": trough and up and quiet, "trough_up_quiet_inst": trough and up and quiet and inst is True,
            "peak": peak, "peak_loud": peak and loud, "peak_loud_up": peak and loud and up,
            "turn_up": tu, "turn_up_up": tu and up, "turn_up_loud": tu and loud, "turn_dn": td, "turn_dn_loud": td and loud,
            # 特徵（平點轉換率用）
            "sig": sig, "quiet": quiet if vr is not None else None, "inst": inst, "streak2": streak2,
            "deep": prev <= -2 * sig, "red": C[i] > C[i - 1]}


def outcome(C: list, i: int, W: int, sig: float) -> str | None:
    """平點（第 i 天）之後的下一段 W 天：轉上／續跌／盤整（事後結果，只用來統計）。"""
    if i + W >= len(C) or C[i + W] is None:
        return None
    nxt = C[i + W] / C[i] - 1
    return "轉上" if nxt >= TURN * sig else "續跌" if nxt <= -TURN * sig else "盤整"


def forward(O: list, C: list, i: int, h: int) -> float | None:
    """第 i 天判定、第 i+1 天開盤買進、第 i+h 天收盤的報酬（%）。"""
    if i + h >= len(C) or not O[i + 1] or C[i + h] is None:
        return None
    return (C[i + h] / O[i + 1] - 1) * 100


def study(db: DB, start: str, end: str, split: str | None = None, windows=(3, 5, 10), horizons=(5, 10)) -> dict:
    trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=? ORDER BY date",
                                       (start, end))]
    if len(trading) < 30:
        return {"error": "期間太短"}
    split = split or trading[len(trading) * 2 // 3]
    qof = lambda d: d[:4] + "%02d" % (((int(d[4:6]) - 1) // 3) * 3 + 1)   # noqa: E731  季別（YYYYMM，季初月）
    qs = sorted({qof(d) for d in trading})
    qstart = {q: next(d for d in trading if d[:6] >= q) for q in qs}
    uni = {q: set(rank_universe(db, qstart[q], BTConfig("", "", ""))) for q in qs}
    codes = sorted(set().union(*uni.values()))
    since = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date<? ORDER BY date DESC LIMIT 90",
                                     (start,))]
    hist = History(db, codes, end, since=since[-1] if since else start)
    conv: dict = {}    # (W, group, part) -> {轉上/續跌/盤整: n}
    feat: dict = {}    # (W, feature, part, 有/無) -> [轉上?]
    acc: dict = {}     # (W, group, h, part) -> [ret]
    dates: dict = {}   # (W, group, part) -> set(date)
    for code in codes:
        ds = hist.qd[code]
        adj = hist.adj[code]
        C = [(adj.get(d) or {}).get("close") for d in ds]
        O = [(adj.get(d) or {}).get("open") for d in ds]
        V = [hist.raw[code][d]["volume"] for d in ds]
        ins = {r["date"]: r for r in hist.ins[code]}
        N = [None if d not in ins else (ins[d]["foreign_net"] or 0) + (ins[d]["trust_net"] or 0) for d in ds]
        lr = log_returns(C)
        for W in windows:
            prev = None
            for i, d in enumerate(ds):
                f = flags(C, V, N, i, W, lr)
                if f is None or not (start <= d <= end) or code not in uni.get(qof(d), ()):
                    prev = f
                    continue
                part = "樣本內" if d < split else "樣本外"
                for _, g in GROUPS:
                    if not f[g] or (g not in ("base", "up") and prev is not None and prev[g]):
                        continue
                    dates.setdefault((W, g, part), set()).add(d)
                    if g.startswith("trough"):
                        o = outcome(C, i, W, f["sig"])
                        if o:
                            cv = conv.setdefault((W, g, part), {"轉上": 0, "續跌": 0, "盤整": 0})
                            cv[o] += 1
                            if g == "trough":
                                for _, k in FEATURES:
                                    if f[k] is not None:
                                        feat.setdefault((W, k, part, bool(f[k])), []).append(o == "轉上")
                    for h in horizons:
                        r = forward(O, C, i, h)
                        if r is not None:
                            acc.setdefault((W, g, h, part), []).append(r)
                prev = f
    rows = []
    for W in windows:
        for label, g in GROUPS:
            for h in horizons:
                for part in ("樣本內", "樣本外"):
                    xs, base = acc.get((W, g, h, part), []), acc.get((W, "base", h, part), [])
                    m = fmean(xs) if xs else None
                    rows.append({"W": W, "group": label, "key": g, "h": h, "part": part, "n": len(xs),
                                 "days": len(dates.get((W, g, part), ())),
                                 "mean": round(m, 2) if m is not None else None,
                                 "up": round(sum(x > 0 for x in xs) / len(xs) * 100, 1) if xs else None,
                                 "excess": round(m - fmean(base), 2) if xs and base else None})
    convs = [{"W": W, "key": g, "part": p, **c} for (W, g, p), c in sorted(conv.items())]
    feats = []
    for W in windows:
        for label, k in FEATURES:
            for p in ("樣本內", "樣本外"):
                yes, no = feat.get((W, k, p, True), []), feat.get((W, k, p, False), [])
                feats.append({"W": W, "feature": label, "key": k, "part": p, "nYes": len(yes), "nNo": len(no),
                              "upYes": round(sum(yes) / len(yes) * 100, 1) if yes else None,
                              "upNo": round(sum(no) / len(no) * 100, 1) if no else None})
    return {"start": trading[0], "end": trading[-1], "split": split, "windows": list(windows), "horizons": list(horizons),
            "stocks": len(codes), "rows": rows, "conversion": convs, "features": feats}


def render(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    out = [f"平點／轉折研究 {r['start']}～{r['end']}，樣本外從 {r['split']} 起，股票 {r['stocks']} 檔",
           "報酬＝隔天開盤買、持有到第 h 天收盤；超額＝減去同一段期間所有股票日子的平均。只算條件成立的第一天。", ""]
    f = lambda v, suf="": "—" if v is None else f"{v:+.2f}{suf}" if suf != "%%" else f"{v:.0f}%"   # noqa: E731
    for W in r["windows"]:
        for h in r["horizons"]:
            out.append(f"── 切段 {W} 天・持有 {h} 日 ──　筆數／平均％／上漲率／超額")
            for label, g in GROUPS:
                cells = []
                for p in ("樣本內", "樣本外"):
                    x = next(x for x in r["rows"] if x["W"] == W and x["key"] == g and x["h"] == h and x["part"] == p)
                    cells.append(f"{p[-1]} {x['n']:>6} {f(x['mean'])} {f(x['up'], '%%')} {f(x['excess'])}")
                out.append(f"{label:<16}| " + " | ".join(cells))
            out.append("")
    out.append("── 平點之後下一段（同樣 W 天）的結果：轉上／盤整／續跌 ──")
    for W in r["windows"]:
        for label, g in GROUPS:
            if not g.startswith("trough"):
                continue
            cells = []
            for p in ("樣本內", "樣本外"):
                c = next((x for x in r["conversion"] if x["W"] == W and x["key"] == g and x["part"] == p), None)
                n = c and (c["轉上"] + c["續跌"] + c["盤整"])
                cells.append(f"{p[-1]} {n or 0:>6} 筆 轉上 {c['轉上'] / n * 100:4.0f}% 盤整 {c['盤整'] / n * 100:4.0f}% 續跌 {c['續跌'] / n * 100:4.0f}%"
                             if n else f"{p[-1]}      0 筆")
            out.append(f"W={W:<2} {label:<16}| " + " | ".join(cells))
    out.append("")
    out.append("── 哪些特徵讓平點比較會轉上（谷：由跌轉平）：有這個特徵／沒有 的轉上率 ──")
    for W in r["windows"]:
        for label, k in FEATURES:
            cells = []
            for p in ("樣本內", "樣本外"):
                x = next(x for x in r["features"] if x["W"] == W and x["key"] == k and x["part"] == p)
                diff = x["upYes"] - x["upNo"] if x["upYes"] is not None and x["upNo"] is not None else None
                cells.append(f"{p[-1]} 有 {x['upYes'] if x['upYes'] is not None else '—':>5}%（{x['nYes']}）"
                             f" 無 {x['upNo'] if x['upNo'] is not None else '—':>5}%（{x['nNo']}）"
                             f" 差 {f'{diff:+.1f}' if diff is not None else '—'}")
            out.append(f"W={W:<2} {label:<18}| " + " | ".join(cells))
    return "\n".join(out)
