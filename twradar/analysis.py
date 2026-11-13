"""指標計算與右側加碼訊號判定（規則與「右側加碼雷達」網頁工具一致）。"""
from __future__ import annotations

import json
from datetime import datetime

from .config import TW
from .db import DB

DEFAULT_RULES = {"maTol": 0.5, "streakDays": 2, "amplify": 2, "volRatio": 1, "marginFlat": 0,
                 "divergeMargin": 0, "blackPct": 3, "blowoff": 1.5}
LIGHT_LABEL = {"go": "加碼訊號", "wait": "觀察中", "stop": "不宜加碼", "na": "資料不足"}


def rules_of(db: DB) -> dict:
    return {**DEFAULT_RULES, **(db.get_setting("rules", {}) or {})}


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


def indicators_many(db: DB, codes: list[str], d: str) -> dict[str, dict]:
    """整份清單的當日指標（欄位名稱與網頁工具相同；缺的欄位不放）。每張表只查一次。"""
    codes = list(dict.fromkeys(codes))
    Q = _latest_rows(db, "quotes", "code, date, close, change, volume", codes, d, 61)
    I = _latest_rows(db, "inst", "code, date, foreign_net, trust_net, dealer_net", codes, d, 30)
    ph = ",".join("?" * len(codes))
    M = {r["code"]: r for r in db.q(f"SELECT * FROM margin WHERE date=? AND code IN ({ph})", (d, *codes))} if codes else {}
    SRC: dict[str, set] = {}
    if codes:
        for t in ("quotes", "inst", "margin"):
            for r in db.q(f"SELECT code, src FROM {t} WHERE date=? AND code IN ({ph})", (d, *codes)):
                if r["src"]:
                    SRC.setdefault(r["code"], set()).add(r["src"])
    return {c: _indicators(Q[c], I[c], M.get(c), SRC.get(c), d) for c in codes}


def indicators(db: DB, code: str, d: str) -> dict:
    return indicators_many(db, [code], d)[code]


def _indicators(q: list, ins: list, m, srcs: set | None, d: str) -> dict:
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
        if len(fs) >= 5 and None not in fs[:5]:
            rec["foreign5d"] = _lots(sum(fs[:5]))
        if len(ts) >= 5 and None not in ts[:5]:
            rec["trust5d"] = _lots(sum(ts[:5]))
    if m:
        if m["margin_bal"] is not None and m["margin_prev"] is not None:
            rec["marginChg"] = m["margin_bal"] - m["margin_prev"]
        if m["short_bal"] is not None and m["short_prev"] is not None:
            rec["shortChg"] = m["short_bal"] - m["short_prev"]
    if srcs:
        rec["source"] = "、".join(sorted(srcs))
    return {k: v for k, v in rec.items() if v is not None}


def _fmt(v, d=0):
    return "—" if v is None else f"{v:,.{d}f}"


def evaluate(rec: dict, stock: dict, R: dict) -> dict:
    g = rec.get
    checks, needs, warns, missing = {}, [], [], []
    fS, tS, f5, t5 = g("foreignStreak"), g("trustStreak"), g("foreign5d"), g("trust5d")

    def amp(x, s):
        if x is None or x <= 0 or s is None:
            return False
        avg = (s - x) / 4
        return x >= R["amplify"] * max(abs(avg), 1)

    # 法人
    if g("foreign") is None and g("trust") is None:
        checks["inst"] = "na"; missing.append("三大法人")
    else:
        why = []
        if fS is not None and fS >= R["streakDays"]: why.append(f"外資連 {fS} 買")
        if tS is not None and tS >= R["streakDays"]: why.append(f"投信連 {tS} 買")
        if amp(g("foreign"), f5): why.append("外資買超放大")
        if amp(g("trust"), t5): why.append("投信買超放大")
        checks["inst"] = "pass" if why else "fail"
        checks["instWhy"] = "、".join(why)
        if not why:
            cur = "" if fS is None else (f"（外資目前連 {fS} 買）" if fS > 0 else f"（外資目前連 {-fS} 賣）" if fS < 0 else "（外資持平）")
            needs.append(f"外資或投信連買 ≥ {R['streakDays']} 日{cur}")
    # 主力
    mf, bd = g("mainForce"), g("brokerDiff")
    if mf is None and bd is None:
        checks["main"] = "na"; missing.append("主力分點")
    else:
        p = (bd is not None and bd < 0) or (mf is not None and mf > 0 and not (bd is not None and bd > 0))
        checks["main"] = "pass" if p else "fail"
        if not p: needs.append("主力分點集中買進（家數差轉負）")
    # 散戶
    if g("marginChg") is None or g("chgPct") is None:
        checks["retail"] = "na"; missing.append("融資")
    else:
        p = g("marginChg") <= R["marginFlat"] and g("chgPct") > 0
        checks["retail"] = "pass" if p else "fail"
        if not p:
            needs.append(f"融資減少（目前 {g('marginChg'):+,} 張）" if g("chgPct") > 0 else "股價上漲且融資不增")
    # 價量
    close, ma20 = g("close"), g("ma20")
    has_pv = close is not None and ma20 is not None
    vol_r = (g("vol") / g("vol5")) if g("vol") is not None and g("vol5") else None
    below = None
    if not has_pv or vol_r is None:
        checks["pv"] = "na"; missing.append("收盤/月線" if not has_pv else "量能")
    if has_pv:
        below = close < ma20 * (1 - R["maTol"] / 100)
        under = close < ma20
        if vol_r is not None:
            vol_up = vol_r > R["volRatio"]
            breakout = g("prevHigh") is not None and close > g("prevHigh")
            p = (not under and vol_up) or (breakout and vol_up)
            checks["pv"] = "pass" if p else "fail"
            if not p:
                needs.append(f"帶量站上月線 {_fmt(ma20, 2)}" if under else f"量能 > 5 日均量（目前 {vol_r:.2f} 倍）")
    # 扣分
    inst_known = g("foreign") is not None or g("trust") is not None
    inst_net = (g("foreign") or 0) + (g("trust") or 0)
    if g("chgPct") is not None and g("chgPct") > 0 and inst_known and inst_net < 0 and g("marginChg") is not None and g("marginChg") > R["divergeMargin"]:
        warns.append("籌碼背離：上漲但法人賣、融資增")
    if below:
        warns.append(f"跌破月線 {_fmt(ma20, 2)}")
    if g("chgPct") is not None and g("chgPct") <= -R["blackPct"] and vol_r is not None and vol_r >= R["blowoff"]:
        warns.append("爆量長黑")
    chips = sum(1 for k in ("inst", "main", "retail") if checks.get(k) == "pass")
    if not rec or (not has_pv and not inst_known):
        light = "na"
    elif warns:
        light = "stop"
    elif checks.get("pv") == "pass" and chips >= 2:
        light = "go"
    else:
        light = "wait"
    if light == "wait" and checks.get("pv") == "pass" and chips < 2:
        needs.append(f"再多 {2 - chips} 項籌碼支持")
    key_note = None
    kl = stock.get("key_level")
    if kl is not None and close is not None:
        above = close >= kl
        key_note = f"關鍵價 {_fmt(kl)}：" + ("已站上" if above else f"尚未站上（差 {_fmt(kl - close, 2)}）")
        if above and vol_r is not None:
            key_note += "，量能確認" if vol_r > R["volRatio"] else "，但量能不足"
    return {"light": light, "lightLabel": LIGHT_LABEL[light],
            "checks": {"inst": checks.get("inst", "na"), "main": checks.get("main", "na"),
                       "retail": checks.get("retail", "na"), "priceVolume": checks.get("pv", "na")},
            "instWhy": checks.get("instWhy", ""), "chipsPassed": chips, "needs": needs, "warnings": warns,
            "missing": missing, "volRatio": None if vol_r is None else round(vol_r, 3),
            "maDistPct": None if not has_pv else round((close / ma20 - 1) * 100, 2),
            "belowMa20": below, "keyLevelNote": key_note}


def run_analysis(db: DB, d: str) -> dict:
    R = rules_of(db)
    run = db.get_run(d)
    crawl_status = run["status"] if run else "not_started"
    summary = {"go": 0, "wait": 0, "stop": 0, "na": 0}
    with db.tx() as c:
        c.execute("DELETE FROM signals WHERE date=?", (d,))
        watch = db.watchlist()
        recs = indicators_many(db, [s["code"] for s in watch], d)
        for s in watch:
            rec = recs[s["code"]]
            sig = evaluate(rec, s, R)
            summary[sig["light"]] += 1
            c.execute("INSERT INTO signals(date,code,light,payload) VALUES(?,?,?,?)",
                      (d, s["code"], sig["light"], json.dumps({"data": rec, "signal": sig}, ensure_ascii=False)))
        status = "complete" if crawl_status == "complete" else "provisional"
        c.execute("INSERT OR REPLACE INTO analysis(date,status,analyzed_at,rules,summary) VALUES(?,?,?,?,?)",
                  (d, status, datetime.now(TW).isoformat(timespec="seconds"), json.dumps(R), json.dumps(summary)))
    return summary
