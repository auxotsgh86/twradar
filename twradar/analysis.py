"""指標計算與每日判定（判定邏輯在 rules.py 與 conditions.py）。"""
from __future__ import annotations

import json
from datetime import datetime

from .config import TW
from .db import DB
from .rules import LIGHT_LABEL, evaluate as rules_evaluate
from .settings import BUILTIN_PARAMS, SettingsStore

DEFAULT_RULES = {p["id"]: p["value"] for p in BUILTIN_PARAMS}


def rules_of(settings: SettingsStore) -> dict:
    """目前的參數值 {id: value}（含使用者自訂參數）。"""
    return {**DEFAULT_RULES, **settings.params()}


def _lots(v):
    return None if v is None else int(round(v / 1000))


def _streak(vals: list) -> int | None:
    if not vals or vals[0] is None:
        return None
    s = (vals[0] > 0) - (vals[0] < 0)
    if s == 0:
        return 0
    n = 0
    for v in vals:
        if v is not None and ((v > 0) - (v < 0)) == s:
            n += 1
        else:
            break
    return s * n


def _latest_rows(db: DB, table: str, cols: str, codes: list[str], d: str, n: int) -> dict[str, list]:
    """每檔取 d（含）之前最近 n 筆，新到舊。一次查詢整份清單。"""
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    rows = db.q(f"SELECT {cols} FROM (SELECT {cols}, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn "
                f"FROM {table} WHERE code IN ({ph}) AND date<=?) WHERE rn<=? ORDER BY code, date DESC", (*codes, d, n))
    out: dict[str, list] = {c: [] for c in codes}
    for r in rows:
        out[r["code"]].append(r)
    return out


def indicators_many(db: DB, codes: list[str], d: str, P: dict | None = None,
                    stocks: dict[str, dict] | None = None) -> dict[str, dict]:
    """整份清單的當日指標（欄位名稱與網頁工具相同；缺的欄位不放）。每張表只查一次。
    P、stocks（觀察清單 {code: 設定}）用於集保大戶／散戶門檻；省略時用預設值。"""
    codes = list(dict.fromkeys(codes))
    P = {**DEFAULT_RULES, **(P or {})}
    stocks = stocks or {}
    Q = _latest_rows(db, "quotes", "code, date, open, high, low, close, change, volume", codes, d, 61)
    I = _latest_rows(db, "inst", "code, date, foreign_net, trust_net, dealer_net", codes, d, 30)
    MH = _latest_rows(db, "margin", "code, date, margin_prev, margin_bal, short_prev, short_bal", codes, d, 60)
    M = {c: rows[0] for c, rows in MH.items() if rows and rows[0]["date"] == d}
    ph = ",".join("?" * len(codes))
    SRC: dict[str, set] = {}
    if codes:
        for t in ("quotes", "inst", "margin"):
            for r in db.q(f"SELECT code, src FROM {t} WHERE date=? AND code IN ({ph})", (d, *codes)):
                if r["src"]:
                    SRC.setdefault(r["code"], set()).add(r["src"])
    H = _holders_rows(db, codes, d)
    out = {}
    for c in codes:
        rec = _indicators(Q[c], I[c], M.get(c), SRC.get(c), d, MH.get(c) or [])
        if H.get(c):
            closes = [r["close"] for r in Q[c] if r["close"] is not None]
            rec.update(_holders(H[c], P, stocks.get(c, {}), sum(closes) / len(closes) if closes else None))
        out[c] = rec
    return out


def _holders_rows(db: DB, codes: list[str], d: str) -> dict[str, list[dict]]:
    """每檔最近兩週（≤ d）的集保分級資料：{code: [本週 {level: row}, 上週 {level: row}]}。"""
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    dates = db.q(f"SELECT code, date FROM (SELECT DISTINCT code, date FROM holders WHERE code IN ({ph}) AND date<=?) "
                 f"ORDER BY code, date DESC", (*codes, d))
    want: dict[str, list[str]] = {}
    for r in dates:
        if len(want.setdefault(r["code"], [])) < 2:
            want[r["code"]].append(r["date"])
    if not want:
        return {}
    alld = sorted({x for v in want.values() for x in v})
    ph2 = ",".join("?" * len(alld))
    by: dict[tuple, dict] = {}
    for r in db.q(f"SELECT * FROM holders WHERE code IN ({ph}) AND date IN ({ph2})", (*codes, *alld)):
        by.setdefault((r["code"], r["date"]), {})[r["level"]] = dict(r)
    return {c: [by[(c, x)] for x in ds if (c, x) in by] for c, ds in want.items()}


def whale_level(P: dict, stock: dict, avg_price: float | None) -> tuple[int, int]:
    """大戶門檻 → (集保分級編號, 實際張數)。個股設定 > 市值模式 > 全域張數；取最接近的分級下限。"""
    from .providers.tdcc import LEVEL_MIN_LOTS
    lots = stock.get("whaleLots")
    if not lots and P.get("whaleAmount") and avg_price:
        lots = P["whaleAmount"] * 1e4 / (avg_price * 1000)
    lots = lots or P.get("whaleLots", 1000)
    i = min(range(1, len(LEVEL_MIN_LOTS)), key=lambda k: (abs(LEVEL_MIN_LOTS[k] - lots), k))
    return i + 1, LEVEL_MIN_LOTS[i]


def retail_levels(P: dict) -> tuple[int, int]:
    """散戶：持股 ≤ retailMaxLots 張 → (最高分級編號, 實際張數上限)。"""
    from .providers.tdcc import LEVEL_MIN_LOTS
    m = P.get("retailMaxLots", 50)
    uppers = LEVEL_MIN_LOTS[1:]  # 第 k 級（1-based）的上限 = 第 k+1 級下限
    k = min(range(len(uppers)), key=lambda i: (abs(uppers[i] - m), i))
    return k + 1, uppers[k]


def _holders(weeks: list[dict], P: dict, stock: dict, avg_price: float | None) -> dict:
    lvl, lots = whale_level(P, stock, avg_price)
    rmax, rlots = retail_levels(P)

    def whale_pct(w):
        tot = (w.get(17) or {}).get("shares")
        s = 0.0
        for k in range(lvl, 16):
            row = w.get(k)
            if not row:
                continue
            if row["pct"] is not None:
                s += row["pct"]
            elif tot:
                s += (row["shares"] or 0) / tot * 100
        return round(s, 2)

    def retail(w):
        return sum((w.get(k) or {}).get("people") or 0 for k in range(1, rmax + 1))
    cur = weeks[0]
    date = next(iter(cur.values()))["date"]
    out = {"holdersDate": date, "whaleLots": lots, "whalePct": whale_pct(cur), "retailLots": rlots, "retailPeople": retail(cur)}
    if len(weeks) > 1:
        prev_date = next(iter(weeks[1].values()))["date"]
        from .dates import to_date
        if (to_date(date) - to_date(prev_date)).days <= 14:  # 只比較相鄰週
            out["whaleChg"] = round(out["whalePct"] - whale_pct(weeks[1]), 2)
            out["retailChg"] = out["retailPeople"] - retail(weeks[1])
    return out


def indicators(db: DB, code: str, d: str) -> dict:
    return indicators_many(db, [code], d)[code]


def _indicators(q: list, ins: list, m, srcs: set | None, d: str, mh: list | None = None) -> dict:
    rec: dict = {}
    if q and q[0]["date"] == d and q[0]["close"] is not None:
        cur = q[0]
        rec["close"] = cur["close"]
        if cur["change"] is not None and cur["close"] - cur["change"]:
            rec["chgPct"] = round(cur["change"] / (cur["close"] - cur["change"]) * 100, 2)
        if cur["volume"] is not None:
            rec["vol"] = round(cur["volume"] / 1000, 1)
        prev = [r for r in q[1:6] if r["volume"] is not None]
        if len(prev) == 5:
            rec["vol5"] = round(sum(r["volume"] for r in prev) / 5000, 1)
        closes = [r["close"] for r in q if r["close"] is not None]
        if len(closes) >= 20:
            rec["ma20"] = round(sum(closes[:20]) / 20, 2)
        if len(closes) >= 60:
            rec["ma60"] = round(sum(closes[:60]) / 60, 2)
        p20 = [r["close"] for r in q[1:21] if r["close"] is not None]
        if len(p20) == 20:
            rec["prevHigh"] = max(p20)
        for k in ("open", "high", "low"):
            if cur[k] is not None:
                rec[k] = cur[k]
        prev_close = cur["close"] - cur["change"] if cur["change"] is not None else None
        if cur["high"] is not None and prev_close:
            rec["highPct"] = round((cur["high"] / prev_close - 1) * 100, 2)
        if None not in (cur["high"], cur["low"], cur["open"]):
            rng = cur["high"] - cur["low"]
            rec["upperShadowPct"] = round((cur["high"] - max(cur["open"], cur["close"])) / rng * 100, 1) if rng > 0 else 0.0
    if ins and ins[0]["date"] == d:
        rec["foreign"] = _lots(ins[0]["foreign_net"])
        rec["trust"] = _lots(ins[0]["trust_net"])
        if ins[0]["dealer_net"] is not None:
            rec["dealer"] = _lots(ins[0]["dealer_net"])
        # 連續天數只在日期連續（以收盤日期序列判斷）時計算
        by = {r["date"]: r for r in ins}
        fs, ts = [], []
        for r0 in q:
            r = by.get(r0["date"])
            if not r:
                break
            fs.append(r["foreign_net"])
            ts.append(r["trust_net"])
        rec["foreignStreak"] = _streak(fs)
        rec["trustStreak"] = _streak(ts)
        rec["instStreak"] = _streak([None if f is None and t is None else (f or 0) + (t or 0) for f, t in zip(fs, ts)])
        if len(fs) >= 5 and None not in fs[:5]:
            rec["foreign5d"] = _lots(sum(fs[:5]))
        if len(ts) >= 5 and None not in ts[:5]:
            rec["trust5d"] = _lots(sum(ts[:5]))
    if m:
        if m["margin_bal"] is not None and m["margin_prev"] is not None:
            rec["marginChg"] = m["margin_bal"] - m["margin_prev"]
        if m["short_bal"] is not None and m["short_prev"] is not None:
            rec["shortChg"] = m["short_bal"] - m["short_prev"]
        if mh:
            # 融資連增天數（日期需與收盤日期連續）
            by = {r["date"]: r for r in mh}
            n = 0
            for r0 in q:
                r = by.get(r0["date"])
                if not r or r["margin_bal"] is None or r["margin_prev"] is None or r["margin_bal"] <= r["margin_prev"]:
                    break
                n += 1
            rec["marginStreak"] = n
            bals = [r["margin_bal"] for r in mh if r["margin_bal"] is not None]
            if len(bals) >= 20 and m["margin_bal"] is not None:
                lo, hi = min(bals), max(bals)
                rec["marginPos60"] = round((m["margin_bal"] - lo) / (hi - lo) * 100, 1) if hi > lo else 0.0
    if srcs:
        rec["source"] = "、".join(sorted(srcs))
    return {k: v for k, v in rec.items() if v is not None}


def evaluate(rec: dict, stock: dict, R: dict, settings: dict | None = None) -> dict:
    """單檔判定（相容舊呼叫方式）。settings 為完整設定 dict；None 時用預設規則。"""
    return rules_evaluate(rec, stock, R, settings)


def _confirm(prev: list[str], raw: str, n: int) -> tuple[str, bool]:
    """訊號確認：新燈號需連續 n 日（含今日）成立才轉換。prev 為前幾日的 (原始燈號, 確認燈號)，新到舊。"""
    if n <= 1 or not prev:
        return raw, False
    confirmed_prev = prev[0][1]
    if raw == confirmed_prev:
        return raw, False
    recent = [p[0] for p in prev[: n - 1]]
    if len(recent) == n - 1 and all(r == raw for r in recent):
        return raw, False
    return confirmed_prev, True


def _previous(db: DB, codes: list[str], d: str, n: int) -> dict[str, list]:
    """前 n 個分析日的訊號（新到舊）。"""
    if n <= 0 or not codes:
        return {}
    dates = [r["date"] for r in db.q("SELECT date FROM analysis WHERE date<? ORDER BY date DESC LIMIT ?", (d, n))]
    if not dates:
        return {}
    out: dict[str, list] = {}
    ph = ",".join("?" * len(dates))
    for r in db.q(f"SELECT date, code, payload FROM signals WHERE date IN ({ph}) ORDER BY date DESC", tuple(dates)):
        sig = json.loads(r["payload"]).get("signal", {})
        out.setdefault(r["code"], []).append(sig)
    return out


def _apply_confirm(sig: dict, prev: list[dict], n: int) -> None:
    sig["rawLight"] = sig["light"]
    light, pending = _confirm([(p.get("rawLight", p.get("light")), p.get("light")) for p in prev], sig["light"], n)
    if pending:
        sig["light"], sig["lightLabel"], sig["pending"] = light, LIGHT_LABEL.get(light, light), sig["rawLight"]
    if "hold" in sig:
        h = sig["hold"]
        h["rawLight"] = h["light"]
        hp = [(p["hold"].get("rawLight", p["hold"]["light"]), p["hold"]["light"]) for p in prev if "hold" in p]
        light, pending = _confirm(hp, h["light"], n)
        if pending:
            h["light"], h["lightLabel"], h["pending"] = light, LIGHT_LABEL.get(light, light), h["rawLight"]


def run_analysis(db: DB, d: str, settings: SettingsStore) -> dict:
    S = settings.get()
    R = {**DEFAULT_RULES, **{p["id"]: p["value"] for p in S["params"]}}
    n_confirm = S.get("confirmDays", 1)
    run = db.get_run(d)
    crawl_status = run["status"] if run else "not_started"
    summary = {"go": 0, "wait": 0, "stop": 0, "na": 0}
    with db.tx() as c:
        c.execute("DELETE FROM signals WHERE date=?", (d,))
        watch = S["watchlist"]
        recs = indicators_many(db, [s["code"] for s in watch], d, R, {s["code"]: s for s in watch})
        prev = _previous(db, [s["code"] for s in watch], d, max(0, n_confirm - 1))
        for s in watch:
            rec = recs[s["code"]]
            sig = evaluate(rec, s, R, S)
            _apply_confirm(sig, prev.get(s["code"], []), n_confirm)
            summary[sig["light"]] += 1
            c.execute("INSERT INTO signals(date,code,light,payload) VALUES(?,?,?,?)",
                      (d, s["code"], sig["light"], json.dumps({"data": rec, "signal": sig}, ensure_ascii=False)))
        status = "complete" if crawl_status == "complete" else "provisional"
        c.execute("INSERT OR REPLACE INTO analysis(date,status,analyzed_at,rules,summary) VALUES(?,?,?,?,?)",
                  (d, status, datetime.now(TW).isoformat(timespec="seconds"), json.dumps(R), json.dumps(summary)))
    return summary
