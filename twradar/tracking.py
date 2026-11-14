"""上線後追蹤（影子組合）：每天用目前的規則判定整個股票池，記下燈號，之後回填 5／10／20 日報酬，
檢查模型最近的表現是否偏離過去（模型失準）。

做法
- 股票池與回溯分析相同（每季依前 60 日成交金額排名：大型、中型、上櫃，另加觀察清單）。
- 一律以「未持有」判定，所以種類只有：上車、否決、大盤逆風、觀察、資料不足。
- 報酬定義與回溯分析相同：訊號隔日開盤買進、持有 h 個交易日後收盤（還原價，未扣費用）。
- 超額報酬＝個股報酬 − 同一天、同組別（大型／中型／上櫃／觀察清單）所有股票的平均報酬。
  同一天的訊號彼此相關，所以先算每天的平均，再算日平均的平均與標準誤。
- 近 20 個交易日（已滿 5 日）的上車超額報酬，比過去的平均低超過 2 個標準誤 → 偏離。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from datetime import date as _date

from .analysis import DEFAULT_RULES, evaluate
from .backtest import HORIZONS, BTConfig, History, rank_universe
from .db import DB

GROUPS = ("大型", "中型", "上櫃", "觀察清單")
WINDOW = 20          # 近期視窗（交易日）
WARMUP = 100         # 計算指標需要往前載入的交易日
MIN_DAYS, MIN_N = 10, 30   # 近期視窗至少要有幾天、幾筆上車訊號才判斷
MIN_REF_DAYS = 20          # 過去至少要有幾天的上車訊號才有比較基準


def kind_of(sig: dict) -> str:
    light, rule = sig.get("light"), sig.get("rule") or ""
    if light == "go":
        return "上車"
    if light == "stop":
        return "否決"
    if light == "wait":
        return "大盤逆風" if rule == "大盤逆風" else "觀察"
    return "資料不足"


def rules_version(S: dict) -> str:
    """規則指紋：參數、條件、規則有任何變更都會改變，用來區分不同版本的訊號。"""
    key = {k: S.get(k) for k in ("rulesVersion", "params", "conditions", "playbooks", "confirmDays")}
    return hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


def _trading(db: DB) -> list[str]:
    return [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]


def _quarter_start(trading: list[str], d: str) -> str:
    q = (d[:4], (int(d[4:6]) - 1) // 3)
    i = bisect.bisect_right(trading, d)
    start = d
    for x in reversed(trading[:i]):
        if (x[:4], (int(x[4:6]) - 1) // 3) != q:
            break
        start = x
    return start


def _back(trading: list[str], d: str, n: int) -> str:
    i = bisect.bisect_left(trading, d)
    return trading[max(0, i - n)] if trading else d


def record(db: DB, S: dict, dates: list[str], progress=None) -> int:
    """判定 dates（交易日）當天的股票池並寫入 track；同一天重跑會覆蓋。回傳寫入筆數。"""
    trading = _trading(db)
    dates = sorted(d for d in set(dates) if d in set(trading))
    if not dates:
        return 0
    P = {**DEFAULT_RULES, **{p["id"]: p["value"] for p in S["params"]}}
    ver, cfg, watch = rules_version(S), BTConfig("", "", ""), [w["code"] for w in S["watchlist"]]
    uni_q: dict[str, dict[str, set[str]]] = {}
    per_day: dict[str, dict[str, set[str]]] = {}
    for d in dates:
        q0 = _quarter_start(trading, d)
        if q0 not in uni_q:
            u = {c: set(t) for c, t in rank_universe(db, q0, cfg).items()}
            for c in watch:
                u.setdefault(c, set()).add("觀察清單")
            uni_q[q0] = u
        per_day[d] = uni_q[q0]
    codes = sorted({c for u in per_day.values() for c in u})
    hist = History(db, codes, dates[-1], since=_back(trading, dates[0], WARMUP))
    n = 0
    for i, d in enumerate(dates):
        rows = []
        for code, tags in per_day[d].items():
            rec = hist.rec(code, d, P)
            if not rec or rec.get("close") is None:
                continue
            sig = evaluate(rec, {"code": code}, P, S)
            grp = next(g for g in GROUPS if g in tags) if tags & set(GROUPS) else "觀察清單"
            fw = hist.fwd(code, d)
            rows.append((d, code, grp, ",".join(sorted(tags)), kind_of(sig), sig.get("rule") or "", ver,
                         *(round(fw[h], 4) if h in fw else None for h in HORIZONS)))
        with db.tx() as c:
            c.execute("DELETE FROM track WHERE date=?", (d,))
            c.executemany("INSERT INTO track(date,code,grp,tags,kind,rule,version,f5,f10,f20) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        n += len(rows)
        if progress:
            progress(i + 1, len(dates), d)
    return n


def update_forward(db: DB, until: str | None = None) -> int:
    """回填之前紀錄的 5／10／20 日報酬（還沒滿期的留空）。回傳更新筆數。"""
    trading = _trading(db)
    if not trading:
        return 0
    until = until or trading[-1]
    lo = _back(trading, until, 30)   # 20 日報酬最多需要 21 個交易日，更早的都已回填
    pend = db.q("SELECT date, code, f5, f10, f20 FROM track WHERE f20 IS NULL AND date>=? AND date<?", (lo, until))
    if not pend:
        return 0
    hist = History(db, sorted({r["code"] for r in pend}), until, since=min(r["date"] for r in pend))
    upd = []
    for r in pend:
        fw = hist.fwd(r["code"], r["date"])
        vals = [round(fw[h], 4) if h in fw else r[f"f{h}"] for h in HORIZONS]
        if vals != [r[f"f{h}"] for h in HORIZONS]:
            upd.append((*vals, r["date"], r["code"]))
    with db.tx() as c:
        c.executemany("UPDATE track SET f5=?, f10=?, f20=? WHERE date=? AND code=?", upd)
    return len(upd)


def daily(db: DB, S: dict, d: str) -> int:
    """每天收盤分析後執行：記錄當天、回填之前的報酬、更新摘要。"""
    n = record(db, S, [d])
    update_forward(db, d)
    refresh(db)
    return n


def refresh(db: DB) -> dict:
    """重算摘要並存起來（狀態頁、晨報直接讀，不用每次重算）。"""
    s = summary(db)
    db.set_setting("tracking", s)
    return s


def cached(db: DB) -> dict:
    return db.get_setting("tracking") or refresh(db)


# ---------------- 摘要 ----------------

def _excess(db: DB) -> dict[str, dict[str, dict[int, list[float]]]]:
    """{種類: {日期: {h: [超額報酬...]}}}；基準＝同日同組別的平均。"""
    base = {(r["date"], r["grp"]): r for r in db.q(
        "SELECT date, grp, AVG(f5) AS m5, AVG(f10) AS m10, AVG(f20) AS m20 FROM track GROUP BY date, grp")}
    out: dict = {}
    for r in db.q("SELECT date, grp, kind, f5, f10, f20 FROM track WHERE f5 IS NOT NULL"):
        b = base[(r["date"], r["grp"])]
        for h in HORIZONS:
            if r[f"f{h}"] is not None and b[f"m{h}"] is not None:
                out.setdefault(r["kind"], {}).setdefault(r["date"], {}).setdefault(h, []).append(r[f"f{h}"] - b[f"m{h}"])
    return out


def _block(by_date: dict[str, dict[int, list[float]]], dates: list[str], h: int) -> dict:
    """日平均的平均與標準誤。"""
    daily_means = [statistics.fmean(by_date[d][h]) for d in dates if d in by_date and by_date[d].get(h)]
    n = sum(len(by_date[d][h]) for d in dates if d in by_date and by_date[d].get(h))
    if not daily_means:
        return {"n": 0, "days": 0}
    out = {"n": n, "days": len(daily_means), "excess": round(statistics.fmean(daily_means), 2)}
    if len(daily_means) >= 2:
        out["se"] = round(statistics.stdev(daily_means) / math.sqrt(len(daily_means)), 2)
    return out


def summary(db: DB, window: int = WINDOW) -> dict:
    first = db.one("SELECT MIN(date) AS a, MAX(date) AS b, COUNT(*) AS n FROM track")
    if not first or not first["n"]:
        return {"status": "未啟用", "text": "還沒有追蹤紀錄（每天收盤分析後會自動記錄；"
                                         "可執行 twradar track --since 20260312 補建過去的紀錄）"}
    ex = _excess(db)
    matured = sorted({r["date"] for r in db.q("SELECT DISTINCT date FROM track WHERE f5 IS NOT NULL")})
    win, ref = matured[-window:], matured[:-window]
    kinds = {}
    for k in ("上車", "否決", "大盤逆風", "觀察"):
        by = ex.get(k, {})
        kinds[k] = {"recent": {f"f{h}": _block(by, win, h) for h in HORIZONS},
                    "past": {f"f{h}": _block(by, ref, h) for h in HORIZONS}}
    go_r, go_p = kinds["上車"]["recent"]["f5"], kinds["上車"]["past"]["f5"]
    if go_r.get("days", 0) < MIN_DAYS or go_r.get("n", 0) < MIN_N:
        status = "樣本不足"
    elif go_p.get("days", 0) < MIN_REF_DAYS:
        status = "累積中"
    elif go_r.get("se") and go_r["excess"] < go_p["excess"] - 2 * go_r["se"]:
        status = "偏離"
    else:
        status = "正常"
    fmt = lambda b: f"{b['excess']:+.2f}%" if b.get("days") else "—"
    text = (f"近 {len(win)} 個交易日上車訊號 {go_r.get('n', 0)} 筆，5 日超額報酬 {fmt(go_r)}"
            f"（過去 {fmt(go_p)}）")
    # 每週：上車訊號數與 5 日超額報酬（已滿期者）
    weeks: dict[str, dict] = {}
    for r in db.q("SELECT date, COUNT(*) AS n FROM track WHERE kind='上車' GROUP BY date ORDER BY date"):
        y, w, _ = _date(int(r["date"][:4]), int(r["date"][4:6]), int(r["date"][6:])).isocalendar()
        wk = weeks.setdefault(f"{y}-W{w:02d}", {"signals": 0, "dates": []})
        wk["signals"] += r["n"]
        wk["dates"].append(r["date"])
    weekly = []
    for wk, v in sorted(weeks.items())[-8:]:
        b = _block(ex.get("上車", {}), v["dates"], 5)
        weekly.append({"week": wk, "signals": v["signals"], "matured": b.get("n", 0), "excess5": b.get("excess")})
    versions = [dict(r) for r in db.q("SELECT version, MIN(date) AS since, MAX(date) AS until, COUNT(*) AS n "
                                      "FROM track GROUP BY version ORDER BY since")]
    return {"status": status, "text": text, "since": first["a"], "latest": first["b"], "rows": first["n"],
            "window": {"from": win[0], "to": win[-1], "days": len(win)} if win else None,
            "pastDays": len(ref), "kinds": kinds, "weekly": weekly, "versions": versions,
            "versionsInWindow": sorted({r["version"] for r in db.q(
                "SELECT DISTINCT version FROM track WHERE date>=?", (win[0],))}) if win else []}


def brief(db: DB) -> dict:
    """給晨報的一行。"""
    s = cached(db)
    return {"status": s["status"], "text": s["text"]}
