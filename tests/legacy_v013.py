"""v0.1.3 的判定邏輯（凍結副本），只用於對照測試：新規則引擎的預設規則必須得到相同結果。"""
LIGHT_LABEL = {"go": "加碼訊號", "wait": "觀察中", "stop": "不宜加碼", "na": "資料不足"}


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
    kl = stock.get("keyLevel")
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
