"""內建條件。

每個條件看一檔股票當日的指標（rec）、觀察清單設定（stock）與參數（P），回傳：
  pass（成立）／fail（不成立）／na（資料不足）＋說明文字、還差什麼。

新增內建條件：寫一個函式並加上 @condition(...)。需要歷史序列或複雜計算的條件寫在這裡；
單純比較（例如「收盤 ≥ 季線」）可以在網頁的設定頁新增「比較條件」，不用寫程式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

PASS, FAIL, NA = "pass", "fail", "na"


@dataclass
class Res:
    state: str
    text: str = ""      # 成立時的說明（例如「外資連 3 買」）
    need: str = ""      # 不成立時「還差什麼」
    value: object = None


@dataclass
class Ctx:
    rec: dict
    stock: dict
    P: dict
    cache: dict = field(default_factory=dict)

    def g(self, k):
        return self.rec.get(k)

    @property
    def vol_r(self):
        v, v5 = self.g("vol"), self.g("vol5")
        return v / v5 if v is not None and v5 else None

    def profit_at(self, price):
        cost = self.stock.get("cost")
        if not cost or price is None:
            return None
        fee, tax = self.P.get("feePct", 0.1425) / 100, self.P.get("taxPct", 0.3) / 100
        return (price * (1 - fee - tax) / (cost * (1 + fee)) - 1) * 100

    @property
    def peak_profit_pct(self):
        """買進日以來最高收盤的報酬率（保本線用）。"""
        return self.profit_at(self.g("peakClose"))

    def lots_amt(self, lots):
        """張數 → 金額（萬元）。"""
        close = self.g("close")
        return None if lots is None or close is None else lots * 1000 * close / 1e4

    @property
    def profit_pct(self):
        """以成本計算的報酬率（%），扣手續費與證交稅估算。未填成本為 None。"""
        cost, close = self.stock.get("cost"), self.g("close")
        if not cost or close is None:
            return None
        fee, tax = self.P.get("feePct", 0.1425) / 100, self.P.get("taxPct", 0.3) / 100
        buy = cost * (1 + fee)
        sell = close * (1 - fee - tax)
        return (sell / buy - 1) * 100


@dataclass(frozen=True)
class Cond:
    id: str
    label: str
    side: str                       # "entry" 加碼面、"hold" 持有面、"any" 兩者
    params: tuple[str, ...]         # 用到的參數 id（設定頁顯示用）
    fields: tuple[str, ...]         # 用到的指標欄位（說明用）
    fn: Callable[[Ctx], Res]
    note: str = ""


BUILTIN: dict[str, Cond] = {}


def condition(id, label, side, params=(), fields=(), note=""):
    def deco(fn):
        if id in BUILTIN:
            raise ValueError(f"條件 id 重複：{id}")
        BUILTIN[id] = Cond(id, label, side, tuple(params), tuple(fields), fn, note)
        return fn
    return deco


def fmt(v, d=0):
    return "—" if v is None else f"{v:,.{d}f}"


def _amp(x, s, P):
    """單日買超放大：當日買超 ≥ 前 4 日平均買賣超（取絕對值）× 倍數。"""
    if x is None or x <= 0 or s is None:
        return False
    avg = (s - x) / 4
    return x >= P["amplify"] * max(abs(avg), 1)


# ---------------- 資料 ----------------

@condition("noData", "資料不足", "any", fields=("close", "ma20", "foreign", "trust"),
           note="沒有收盤／月線，也沒有法人資料")
def no_data(c: Ctx) -> Res:
    has_pv = c.g("close") is not None and c.g("ma20") is not None
    inst = c.g("foreign") is not None or c.g("trust") is not None
    return Res(PASS if not c.rec or (not has_pv and not inst) else FAIL)


# ---------------- 加碼面 ----------------

@condition("inst", "法人買超", "entry", ("streakDays", "amplify"),
           ("foreignStreak", "trustStreak", "foreign", "trust", "foreign5d", "trust5d"),
           "外資或投信連買達天數；或任一方買超放大")
def inst(c: Ctx) -> Res:
    g, P = c.g, c.P
    if g("foreign") is None and g("trust") is None:
        return Res(NA)
    fS, tS = g("foreignStreak"), g("trustStreak")
    why = []
    if fS is not None and fS >= P["streakDays"]: why.append(f"外資連 {fS} 買")
    if tS is not None and tS >= P["streakDays"]: why.append(f"投信連 {tS} 買")
    if _amp(g("foreign"), g("foreign5d"), P): why.append("外資買超放大")
    if _amp(g("trust"), g("trust5d"), P): why.append("投信買超放大")
    if why:
        return Res(PASS, "、".join(why))
    cur = "" if fS is None else (f"（外資目前連 {fS} 買）" if fS > 0 else f"（外資目前連 {-fS} 賣）" if fS < 0 else "（外資持平）")
    return Res(FAIL, need=f"外資或投信連買 ≥ {fmt(P['streakDays'])} 日{cur}")


@condition("main", "主力集中", "entry", fields=("mainForce", "brokerDiff"), note="券商分點集中買進（目前無資料源）")
def main_force(c: Ctx) -> Res:
    mf, bd = c.g("mainForce"), c.g("brokerDiff")
    if mf is None and bd is None:
        return Res(NA)
    p = (bd is not None and bd < 0) or (mf is not None and mf > 0 and not (bd is not None and bd > 0))
    return Res(PASS, "主力集中買進") if p else Res(FAIL, need="主力分點集中買進（家數差轉負）")


@condition("retail", "散戶退場", "entry", ("marginFlat",), ("marginChg", "chgPct"), "收紅且融資不增")
def retail(c: Ctx) -> Res:
    m, ch = c.g("marginChg"), c.g("chgPct")
    if m is None or ch is None:
        return Res(NA)
    if m <= c.P["marginFlat"] and ch > 0:
        return Res(PASS, f"收紅、融資 {m:+,} 張")
    return Res(FAIL, need=f"融資減少（目前 {m:+,} 張）" if ch > 0 else "股價上漲且融資不增")


@condition("pvConfirm", "價量確認", "entry", ("volRatio",), ("close", "ma20", "vol", "vol5", "prevHigh"),
           "帶量站上月線，或帶量突破前高")
def pv_confirm(c: Ctx) -> Res:
    close, ma20, vr = c.g("close"), c.g("ma20"), c.vol_r
    if close is None or ma20 is None or vr is None:
        return Res(NA)
    under = close < ma20
    vol_up = vr > c.P["volRatio"]
    breakout = c.g("prevHigh") is not None and close > c.g("prevHigh")
    if (not under and vol_up) or (breakout and vol_up):
        return Res(PASS, ("突破前高" if breakout else "站上月線") + f"、量比 {vr:.2f}")
    return Res(FAIL, need=f"帶量站上月線 {fmt(ma20, 2)}" if under else f"量能 > 5 日均量（目前 {vr:.2f} 倍）")


@condition("divergence", "籌碼背離", "entry", ("divergeMargin",), ("chgPct", "foreign", "trust", "marginChg"),
           "收紅，但外資＋投信合計賣超，且融資增加")
def divergence(c: Ctx) -> Res:
    g = c.g
    known = g("foreign") is not None or g("trust") is not None
    if g("chgPct") is None or not known or g("marginChg") is None:
        return Res(NA)
    net = (g("foreign") or 0) + (g("trust") or 0)
    p = g("chgPct") > 0 and net < 0 and g("marginChg") > c.P["divergeMargin"]
    return Res(PASS, "籌碼背離：上漲但法人賣、融資增") if p else Res(FAIL)


@condition("belowMa20", "跌破月線", "any", ("maTol",), ("close", "ma20"), "收盤低於月線超過容許誤差")
def below_ma20(c: Ctx) -> Res:
    close, ma20 = c.g("close"), c.g("ma20")
    if close is None or ma20 is None:
        return Res(NA)
    lim = ma20 * (1 - c.P["maTol"] / 100)
    return Res(PASS, f"跌破月線 {fmt(ma20, 2)}") if close < lim else Res(FAIL, need=f"守住 {fmt(lim, 2)}")


@condition("blowoffBlack", "爆量長黑", "any", ("blackPct", "blowoff"), ("chgPct", "vol", "vol5"))
def blowoff_black(c: Ctx) -> Res:
    ch, vr = c.g("chgPct"), c.vol_r
    if ch is None or vr is None:
        return Res(NA)
    return Res(PASS, "爆量長黑") if ch <= -c.P["blackPct"] and vr >= c.P["blowoff"] else Res(FAIL)


@condition("instConflict", "法人對作", "entry", ("conflictRatio", "conflictMinAmt"), ("foreign", "trust", "close"),
           "外資與投信一買一賣，較小一方的張數 ≥ 較大一方的設定比例，且金額達門檻")
def inst_conflict(c: Ctx) -> Res:
    f, t, close = c.g("foreign"), c.g("trust"), c.g("close")
    if f is None or t is None:
        return Res(NA)
    if f * t >= 0:
        return Res(FAIL)
    big, small = max(abs(f), abs(t)), min(abs(f), abs(t))
    amt = small * 1000 * close / 1e4 if close else None  # 萬元
    if small >= big * c.P["conflictRatio"] / 100 and (amt is None or amt >= c.P["conflictMinAmt"]):
        who = "外資買、投信賣" if f > 0 else "投信買、外資賣"
        return Res(PASS, f"法人對作：{who}（外資 {f:+,}、投信 {t:+,} 張）")
    return Res(FAIL)


# ---------------- 持有面 ----------------

@condition("stopLoss", "停損", "hold", ("stopLossPct", "feePct", "taxPct"), ("close",), "以成本計虧損達停損比例")
def stop_loss(c: Ctx) -> Res:
    p = c.profit_pct
    if p is None:
        return Res(NA)
    return Res(PASS, f"虧損 {p:.1f}% 達停損 {fmt(c.P['stopLossPct'])}%", value=p) if p <= -c.P["stopLossPct"] else Res(FAIL, value=p)


@condition("profitTarget", "獲利達標", "hold", ("profitTargetPct", "feePct", "taxPct"), ("close",),
           "以成本計獲利達門檻，啟動獲利保護")
def profit_target(c: Ctx) -> Res:
    p = c.profit_pct
    if p is None:
        return Res(NA)
    if p >= c.P["profitTargetPct"]:
        return Res(PASS, f"獲利 {p:.1f}%", value=p)
    return Res(FAIL, need=f"獲利 ≥ {fmt(c.P['profitTargetPct'])}%（目前 {p:.1f}%）", value=p)


@condition("belowKeyLevel", "跌破關鍵價", "hold", (), ("close",), "收盤低於觀察清單設定的關鍵價")
def below_key(c: Ctx) -> Res:
    kl, close = c.stock.get("keyLevel"), c.g("close")
    if kl is None or close is None:
        return Res(NA)
    return Res(PASS, f"跌破關鍵價 {fmt(kl)}") if close < kl else Res(FAIL)


@condition("instSellStreak", "法人連賣", "hold", ("sellStreakDays",), ("instStreak",), "外資＋投信合計連續賣超")
def inst_sell(c: Ctx) -> Res:
    s = c.g("instStreak")
    if s is None:
        return Res(NA)
    n = c.P["sellStreakDays"]
    return Res(PASS, f"法人合計連 {-s} 賣") if s <= -n else Res(FAIL)


@condition("whaleDown", "大戶減碼", "hold", ("whaleLots", "whaleAmount", "whaleDropPts"), ("whaleChg", "whalePct"),
           "大戶持股比例較上週減少（集保股權分散表）")
def whale_down(c: Ctx) -> Res:
    w = c.g("whaleChg")
    if w is None:
        return Res(NA)
    lots = c.g("whaleLots")
    if w < 0 and -w >= c.P.get("whaleDropPts", 0):
        return Res(PASS, f"{fmt(lots)} 張以上大戶持股 {w:+.2f} 個百分點（{c.g('whalePct'):.2f}%）")
    return Res(FAIL)


@condition("whaleUp", "大戶增加", "any", ("whaleLots", "whaleAmount"), ("whaleChg", "whalePct"),
           "大戶持股比例較上週增加（未放入預設規則，可自行加入加碼條件）")
def whale_up(c: Ctx) -> Res:
    w = c.g("whaleChg")
    if w is None:
        return Res(NA)
    if w > 0:
        return Res(PASS, f"{fmt(c.g('whaleLots'))} 張以上大戶持股 {w:+.2f} 個百分點")
    return Res(FAIL, need="大戶持股比例週增")


@condition("retailUp", "散戶增加", "hold", ("retailUpPct", "marginUpDays"), ("holdersChgPct", "marginStreak"),
           "集保總股東人數週增達門檻（籌碼發散），或融資連續增加")
def retail_up(c: Ctx) -> Res:
    r, ms = c.g("holdersChgPct"), c.g("marginStreak")
    if r is None and ms is None:
        return Res(NA)
    if r is not None and r >= c.P["retailUpPct"]:
        return Res(PASS, f"股東人數週增 {r:+.2f}%")
    if ms is not None and ms >= c.P["marginUpDays"]:
        return Res(PASS, f"融資連 {ms} 日增加")
    return Res(FAIL)


@condition("marginHigh", "融資偏高", "hold", ("marginHighPct",), ("marginPos60",),
           "融資餘額位於近 60 日區間的高檔")
def margin_high(c: Ctx) -> Res:
    pos, rng, tot = c.g("marginPos60"), c.g("marginRange60"), c.g("totalShares")
    if pos is None:
        return Res(NA)
    # 防呆：60 日融資變化太小（例如 100 → 105 張）不算偏高
    if rng is not None:
        big = rng >= c.P["marginRangeMin"] or (tot and rng >= tot / 1000 * c.P["marginRangePct"] / 100)
        if not big:
            return Res(FAIL)
    return Res(PASS, f"融資位於 60 日區間 {pos:.0f}%") if pos >= c.P["marginHighPct"] else Res(FAIL)


@condition("failedLimitUp", "衝高未鎖漲停", "hold", ("limitNearPct", "shadowPct", "volRatio"),
           ("highPct", "upperShadowPct", "close", "high", "vol", "vol5"), "盤中接近漲停、收盤未鎖、長上影、量縮")
def failed_limit_up(c: Ctx) -> Res:
    hp, sh, close, high, vr = c.g("highPct"), c.g("upperShadowPct"), c.g("close"), c.g("high"), c.vol_r
    if None in (hp, sh, close, high, vr):
        return Res(NA)
    p = hp >= c.P["limitNearPct"] and close < high and sh >= c.P["shadowPct"] and vr <= c.P["volRatio"]
    return Res(PASS, f"盤中漲 {hp:.1f}% 未鎖、上影 {sh:.0f}%、量比 {vr:.2f}") if p else Res(FAIL)


@condition("limitUpOpenBlowoff", "爆量打開漲停", "hold", ("limitTouchPct", "openBlowoff"),
           ("highPct", "close", "high", "vol", "vol5"), "盤中觸及漲停、收盤未鎖，且爆量")
def limit_open(c: Ctx) -> Res:
    hp, close, high, vr = c.g("highPct"), c.g("close"), c.g("high"), c.vol_r
    if None in (hp, close, high, vr):
        return Res(NA)
    p = hp >= c.P["limitTouchPct"] and close < high and vr >= c.P["openBlowoff"]
    return Res(PASS, f"盤中漲 {hp:.1f}% 打開、量比 {vr:.2f}") if p else Res(FAIL)


@condition("ma20BiasHigh", "月線乖離過大", "hold", ("biasHighPct",), ("close", "ma20"))
def bias_high(c: Ctx) -> Res:
    close, ma20 = c.g("close"), c.g("ma20")
    if close is None or not ma20:
        return Res(NA)
    b = (close / ma20 - 1) * 100
    return Res(PASS, f"月線乖離 {b:.1f}%") if b >= c.P["biasHighPct"] else Res(FAIL)


# ================= v0.3（2026-11-13 專家審閱後） =================

def _amp_v3(x, s5, c: Ctx) -> bool:
    """買超放大（v0.3）：前 4 日合計為淨買超、當日 ≥ 前 4 日平均 × 倍數，且當日買超佔成交量或金額達門檻。"""
    if x is None or x <= 0 or s5 is None:
        return False
    prior = s5 - x
    if prior <= 0 or x < c.P["amplify"] * prior / 4:
        return False
    vol, amt = c.g("vol"), c.lots_amt(x)
    return (bool(vol) and x / vol * 100 >= c.P["amplifyVolPct"]) or (amt is not None and amt >= c.P["amplifyMinAmt"])


@condition("instBuy", "法人買超", "entry", ("streakDays", "amplify", "amplifyVolPct", "amplifyMinAmt"),
           ("foreignStreak", "trustStreak", "foreign", "trust", "foreign5d", "trust5d", "vol"),
           "外資或投信連買達天數；或買超放大（前 4 日為淨買、當日佔成交量或金額達門檻）")
def inst_buy(c: Ctx) -> Res:
    g, P = c.g, c.P
    if g("foreign") is None and g("trust") is None:
        return Res(NA)
    fS, tS = g("foreignStreak"), g("trustStreak")
    why = []
    if fS is not None and fS >= P["streakDays"]: why.append(f"外資連 {fS} 買")
    if tS is not None and tS >= P["streakDays"]: why.append(f"投信連 {tS} 買")
    if _amp_v3(g("foreign"), g("foreign5d"), c): why.append("外資買超放大")
    if _amp_v3(g("trust"), g("trust5d"), c): why.append("投信買超放大")
    if why:
        return Res(PASS, "、".join(why))
    cur = "" if fS is None else (f"（外資目前連 {fS} 買）" if fS > 0 else f"（外資目前連 {-fS} 賣）" if fS < 0 else "（外資持平）")
    return Res(FAIL, need=f"外資或投信連買 ≥ {fmt(P['streakDays'])} 日{cur}")


@condition("instTurn", "法人由賣轉買", "entry", (), ("instTurn",), "外資或投信由賣轉買第 1～2 天")
def inst_turn(c: Ctx) -> Res:
    t = c.g("instTurn")
    if t is None:
        return Res(NA)
    return Res(PASS, "法人由賣轉買") if t else Res(FAIL, need="法人由賣轉買")


@condition("trendUp", "月線走平或上揚", "any", (), ("ma20", "ma20Prev", "ma5"), "今日月線 ≥ 昨日月線，或 5 日線 > 月線")
def trend_up(c: Ctx) -> Res:
    ma20, prev, ma5 = c.g("ma20"), c.g("ma20Prev"), c.g("ma5")
    if ma20 is None or (prev is None and ma5 is None):
        return Res(NA)
    if (prev is not None and ma20 >= prev) or (ma5 is not None and ma5 > ma20):
        return Res(PASS, "月線翻揚" if prev is not None and ma20 > prev else "月線走平或 5 日線在上")
    return Res(FAIL, need=f"月線止跌翻揚（今 {fmt(ma20, 2)}／昨 {fmt(prev, 2)}）")


@condition("pvTrend", "價量確認", "entry", ("volRatio",), ("close", "ma20", "ma20Prev", "vol", "vol5", "prevHigh"),
           "帶量站上月線或突破前高，且月線走平或上揚")
def pv_trend(c: Ctx) -> Res:
    close, ma20, vr = c.g("close"), c.g("ma20"), c.vol_r
    if close is None or ma20 is None or vr is None:
        return Res(NA)
    t = trend_up(c)
    breakout = c.g("prevHigh") is not None and close > c.g("prevHigh")
    ok = (close >= ma20 or breakout) and vr > c.P["volRatio"] and t.state == PASS
    if ok:
        return Res(PASS, ("突破前高" if breakout else "站上月線") + f"、量比 {vr:.2f}、{t.text}")
    if close < ma20 and not breakout:
        return Res(FAIL, need=f"帶量站上月線 {fmt(ma20, 2)}")
    if vr <= c.P["volRatio"]:
        return Res(FAIL, need=f"量能 > 5 日均量（目前 {vr:.2f} 倍）")
    return Res(FAIL, need=t.need or "月線走平或上揚")


@condition("pvBreakout", "帶量突破前高", "entry", ("volRatio",), ("close", "prevHigh", "vol", "vol5", "ma20", "ma20Prev"),
           "收盤突破前 20 日最高收盤、量比達門檻，且月線走平或上揚")
def pv_breakout(c: Ctx) -> Res:
    close, ph, vr = c.g("close"), c.g("prevHigh"), c.vol_r
    if close is None or ph is None or vr is None:
        return Res(NA)
    t = trend_up(c)
    if close > ph and vr > c.P["volRatio"] and t.state == PASS:
        return Res(PASS, f"帶量突破前高 {fmt(ph, 2)}（量比 {vr:.2f}）")
    if close <= ph:
        return Res(FAIL, need=f"帶量突破前高 {fmt(ph, 2)}（差 {fmt(ph - close, 2)}）")
    if vr <= c.P["volRatio"]:
        return Res(FAIL, need=f"突破時量能 > 5 日均量（目前 {vr:.2f} 倍）")
    return Res(FAIL, need=t.need or "月線走平或上揚")


@condition("pullbackHeld", "回測月線有守", "entry", ("pullbackDays", "pullbackPct", "pullbackBreakPct"),
           ("pbLowMinPct", "pbCloseMinPct"), "近 N 日曾回測到月線附近，且收盤沒有跌破月線超過容許幅度")
def pullback_held(c: Ctx) -> Res:
    lo, cl = c.g("pbLowMinPct"), c.g("pbCloseMinPct")
    if lo is None or cl is None:
        return Res(NA)
    n = fmt(c.P["pullbackDays"])
    if lo > c.P["pullbackPct"]:
        return Res(FAIL, need=f"近 {n} 日回測月線（最低價離月線最近 {lo:+.1f}%）")
    if cl < -c.P["pullbackBreakPct"]:
        return Res(FAIL, need=f"回測期間收盤守住月線（曾跌破 {cl:.1f}%）")
    return Res(PASS, f"近 {n} 日回測月線有守")


@condition("redVolK", "帶量紅K", "entry", ("volRatio",), ("candle", "vol", "vol5"), "收盤高於開盤，且量比達門檻")
def red_vol_k(c: Ctx) -> Res:
    k, vr = c.g("candle"), c.vol_r
    if k is None or vr is None:
        return Res(NA)
    if k > 0 and vr > c.P["volRatio"]:
        return Res(PASS, f"帶量紅K（量比 {vr:.2f}）")
    return Res(FAIL, need="帶量紅K" if k <= 0 else f"紅K 量能 > 5 日均量（目前 {vr:.2f} 倍）")


@condition("aboveMa5And20", "站上 5 日線與月線", "entry", (), ("close", "ma5", "ma20"))
def above_5_20(c: Ctx) -> Res:
    close, ma5, ma20 = c.g("close"), c.g("ma5"), c.g("ma20")
    if None in (close, ma5, ma20):
        return Res(NA)
    if close > ma5 and close > ma20:
        return Res(PASS, "站上 5 日線與月線")
    return Res(FAIL, need=f"收盤站上 5 日線 {fmt(ma5, 2)} 與月線 {fmt(ma20, 2)}")


@condition("retailHealthy", "融資健康", "entry", ("marginFlat", "marginInstRatio"), ("marginChg", "chgPct", "instNet"),
           "收紅，且融資不增；或融資增加小於法人買超的設定比例")
def retail_healthy(c: Ctx) -> Res:
    m, ch, net = c.g("marginChg"), c.g("chgPct"), c.g("instNet")
    if m is None or ch is None:
        return Res(NA)
    if ch > 0 and m <= c.P["marginFlat"]:
        return Res(PASS, f"收紅、融資 {m:+,} 張")
    lim = (net or 0) * c.P["marginInstRatio"] / 100
    if ch > 0 and net is not None and net > 0 and m < lim:
        return Res(PASS, f"融資 {m:+,} 張 < 法人買超的 {fmt(c.P['marginInstRatio'])}%")
    return Res(FAIL, need=f"融資增加 < 法人買超 × {fmt(c.P['marginInstRatio'])}%（融資 {m:+,}、法人 {fmt(net)}）"
               if ch > 0 else "收紅")


@condition("marginExcess", "融資追價過熱", "entry", (), ("marginChg", "instNet"), "法人買超，但融資增加超過法人買超張數")
def margin_excess(c: Ctx) -> Res:
    m, net = c.g("marginChg"), c.g("instNet")
    if m is None or net is None:
        return Res(NA)
    if net > 0 and m > net:
        return Res(PASS, f"融資追價：融資 {m:+,} 張 > 法人買超 {net:,} 張")
    return Res(FAIL)


@condition("marginDump", "法人賣、融資接", "any", ("divergeMargin", "shadowPct"), ("instNet", "marginChg", "candle", "upperShadowPct"),
           "收黑或留長上影，外資＋投信合計賣超，且融資增加")
def margin_dump(c: Ctx) -> Res:
    m, net, k, sh = c.g("marginChg"), c.g("instNet"), c.g("candle"), c.g("upperShadowPct")
    if m is None or net is None or k is None:
        return Res(NA)
    weak = k < 0 or (sh is not None and sh >= c.P["shadowPct"])
    if weak and net < 0 and m > c.P["divergeMargin"]:
        return Res(PASS, f"法人賣 {net:,} 張、融資增 {m:+,} 張（{'收黑' if k < 0 else '長上影'}）")
    return Res(FAIL)


def _conflict_amt_ok(c: Ctx, small_lots) -> bool:
    amt, amt5 = c.lots_amt(small_lots), c.g("amt5")
    if amt is None:
        return True
    if amt5:
        return amt >= amt5 * c.P["conflictValuePct"] / 100
    return amt >= c.P["conflictMinAmt"]


@condition("trustSellConflict", "外資買、投信賣", "entry", ("conflictRatio", "conflictValuePct", "conflictMinAmt"),
           ("foreign", "trust", "close", "amt5"), "外資買超、投信賣超，投信賣超 ≥ 外資買超的設定比例，且金額達 5 日均成交金額的設定比例")
def trust_sell_conflict(c: Ctx) -> Res:
    f, t = c.g("foreign"), c.g("trust")
    if f is None or t is None:
        return Res(NA)
    if f > 0 and t < 0 and -t >= f * c.P["conflictRatio"] / 100 and _conflict_amt_ok(c, -t):
        return Res(PASS, f"投信賣、外資買（投信 {t:+,}、外資 {f:+,} 張）")
    return Res(FAIL)


@condition("trustBuyConflict", "投信買、外資賣", "entry", ("conflictRatio", "conflictValuePct", "conflictMinAmt"),
           ("foreign", "trust", "close", "amt5"), "投信買超、外資賣超且達門檻：可能是投信認養，先列觀察")
def trust_buy_conflict(c: Ctx) -> Res:
    f, t = c.g("foreign"), c.g("trust")
    if f is None or t is None:
        return Res(NA)
    if t > 0 and f < 0 and min(t, -f) >= max(t, -f) * c.P["conflictRatio"] / 100 and _conflict_amt_ok(c, min(t, -f)):
        return Res(PASS, f"投信買、外資賣（投信 {t:+,}、外資 {f:+,} 張）")
    return Res(FAIL)


@condition("marketWeak", "大盤逆風", "any", (), ("mktClose", "mktMa20", "mktMa20Prev"), "加權指數收盤低於月線，且月線下彎")
def market_weak(c: Ctx) -> Res:
    x, ma, prev = c.g("mktClose"), c.g("mktMa20"), c.g("mktMa20Prev")
    if None in (x, ma, prev):
        return Res(NA)
    if x < ma and ma < prev:
        return Res(PASS, f"大盤逆風：加權 {fmt(x, 2)} 低於下彎月線 {fmt(ma, 2)}")
    return Res(FAIL)


@condition("isHolding", "持有中", "any", (), (), "觀察清單標記為持有")
def is_holding(c: Ctx) -> Res:
    return Res(PASS, "持有中") if c.stock.get("holding") else Res(FAIL, need="（未持有：看上車條件）")


@condition("notHolding", "未持有", "any", (), (), "觀察清單未標記持有")
def not_holding(c: Ctx) -> Res:
    return Res(FAIL, need="（已持有：看加碼條件）") if c.stock.get("holding") else Res(PASS, "未持有")


@condition("addProfitOk", "底倉已獲利", "entry", ("addMinProfit", "feePct", "taxPct"), ("close",),
           "加碼前提：底倉帳面獲利達門檻（不攤平）")
def add_profit_ok(c: Ctx) -> Res:
    p = c.profit_pct
    if p is None:
        return Res(NA)
    if p >= c.P["addMinProfit"]:
        return Res(PASS, f"底倉獲利 {p:.1f}%")
    return Res(FAIL, need=f"底倉獲利 ≥ {fmt(c.P['addMinProfit'])}% 才加碼（目前 {p:.1f}%，不攤平）")


@condition("breakMa20Hard", "有效跌破下彎月線", "hold", ("breakMaPct",), ("close", "ma20", "ma20Prev"),
           "收盤低於月線超過設定幅度，且月線下彎")
def break_ma20_hard(c: Ctx) -> Res:
    close, ma20, prev = c.g("close"), c.g("ma20"), c.g("ma20Prev")
    if None in (close, ma20, prev):
        return Res(NA)
    if close < ma20 * (1 - c.P["breakMaPct"] / 100) and ma20 < prev:
        return Res(PASS, f"跌破下彎月線 {fmt(ma20, 2)} 超過 {fmt(c.P['breakMaPct'], 1)}%")
    return Res(FAIL)


@condition("instNetSell", "法人當日賣超", "any", (), ("instNet",), "外資＋投信合計當日賣超")
def inst_net_sell(c: Ctx) -> Res:
    n = c.g("instNet")
    if n is None:
        return Res(NA)
    return Res(PASS, f"法人合計賣超 {n:,} 張") if n < 0 else Res(FAIL)


@condition("giveback", "保本線", "hold", ("givebackPeak", "givebackFloor", "feePct", "taxPct"), ("close", "peakClose"),
           "買進後最高獲利曾達門檻，現在回吐到保本線以下（需填成本與買進日）")
def giveback(c: Ctx) -> Res:
    peak, now = c.peak_profit_pct, c.profit_pct
    if peak is None or now is None:
        return Res(NA)
    if peak >= c.P["givebackPeak"] and now <= c.P["givebackFloor"]:
        return Res(PASS, f"保本：最高曾獲利 {peak:.1f}%，現在 {now:.1f}%")
    return Res(FAIL)


@condition("failedHigh", "高檔爆量留長上影", "hold", ("limitNearPct", "shadowPct", "failedVol"),
           ("highPct", "upperShadowPct", "close", "open", "high", "vol", "vol5"),
           "盤中大漲後被打下、留長上影線，且出量（拉高出貨）")
def failed_high(c: Ctx) -> Res:
    hp, sh, close, high, op, vr = c.g("highPct"), c.g("upperShadowPct"), c.g("close"), c.g("high"), c.g("open"), c.vol_r
    if None in (hp, sh, close, high, vr):
        return Res(NA)
    body = abs(close - op) if op is not None else 0
    long_shadow = sh >= c.P["shadowPct"] or (op is not None and high - max(op, close) >= 1.5 * body > 0)
    if hp >= c.P["limitNearPct"] and close < high and long_shadow and vr >= c.P["failedVol"]:
        return Res(PASS, f"盤中漲 {hp:.1f}% 被打下、長上影、量比 {vr:.2f}")
    return Res(FAIL)


@condition("biasTurn", "乖離過大且轉弱", "hold", ("biasHighPct", "biasHighLargePct", "largeCapAmt", "shadowPct"),
           ("close", "ma20", "candle", "upperShadowPct", "capAmt"), "月線乖離過大（大型股門檻較低），且收黑或留上影線")
def bias_turn(c: Ctx) -> Res:
    close, ma20, k, sh = c.g("close"), c.g("ma20"), c.g("candle"), c.g("upperShadowPct")
    if close is None or not ma20 or k is None:
        return Res(NA)
    large = (c.g("capAmt") or 0) >= c.P["largeCapAmt"]
    lim = c.P["biasHighLargePct"] if large else c.P["biasHighPct"]
    b = (close / ma20 - 1) * 100
    if b >= lim and (k < 0 or (sh is not None and sh >= c.P["shadowPct"])):
        return Res(PASS, f"月線乖離 {b:.1f}%（{'大型股' if large else ''}門檻 {fmt(lim)}%）且{'收黑' if k < 0 else '留上影'}")
    return Res(FAIL)


@condition("highBlowoffBlack", "高檔爆量長黑", "hold", ("highZonePct", "highBlackPct", "highBlackVol"),
           ("prevFromHigh20", "chgPct", "vol", "vol5", "instNet"), "位於近 20 日高檔，爆量長黑，且法人賣超")
def high_blowoff_black(c: Ctx) -> Res:
    pf, ch, vr, net = c.g("prevFromHigh20"), c.g("chgPct"), c.vol_r, c.g("instNet")
    if None in (pf, ch, vr, net):
        return Res(NA)
    if pf >= -c.P["highZonePct"] and ch <= -c.P["highBlackPct"] and vr >= c.P["highBlackVol"] and net < 0:
        return Res(PASS, f"高檔爆量長黑（{ch:.1f}%、量比 {vr:.2f}、法人賣 {net:,}）")
    return Res(FAIL)


# ---------------- 比較條件（網頁新增） ----------------

COMPARE_FIELDS = {
    "close": "收盤", "open": "開盤", "high": "最高", "low": "最低", "chgPct": "漲跌幅%", "vol": "成交量（張）",
    "vol5": "5 日均量（張）", "volRatio": "量比", "ma20": "月線", "ma60": "季線", "prevHigh": "前高",
    "maDistPct": "月線乖離%", "highPct": "盤中最高漲幅%", "upperShadowPct": "上影線佔振幅%",
    "foreign": "外資買賣超（張）", "trust": "投信買賣超（張）", "dealer": "自營商買賣超（張）",
    "foreignStreak": "外資連買（負為連賣）", "trustStreak": "投信連買（負為連賣）", "instStreak": "法人合計連買（負為連賣）",
    "foreign5d": "外資 5 日合計（張）", "trust5d": "投信 5 日合計（張）",
    "ma5": "5 日線", "ma20Prev": "昨日月線", "amt": "成交金額（萬元）", "amt5": "5 日均成交金額（萬元）",
    "instNet": "外資＋投信買賣超（張）", "holdersTotal": "集保股東人數", "holdersChgPct": "股東人數週增減%",
    "mktClose": "加權指數", "mktMa20": "加權指數月線", "capAmt": "市值（億元）", "peakClose": "買進後最高收盤",
    "marginChg": "融資增減（張）", "shortChg": "融券增減（張）", "marginStreak": "融資連增天數", "marginPos60": "融資 60 日位置%",
    "whalePct": "大戶持股比例%", "whaleChg": "大戶持股週增減（百分點）", "retailPeople": "散戶人數", "retailChg": "散戶人數週增減",
    "keyLevel": "關鍵價", "cost": "成本價", "profitPct": "持有報酬%",
}
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def field_value(c: Ctx, name: str):
    if name == "volRatio":
        return c.vol_r
    if name == "maDistPct":
        close, ma20 = c.g("close"), c.g("ma20")
        return (close / ma20 - 1) * 100 if close is not None and ma20 else None
    if name in ("keyLevel", "cost"):
        return c.stock.get(name)
    if name == "profitPct":
        return c.profit_pct
    return c.g(name)


def operand(c: Ctx, x):
    """比較式的一邊：數字、{"field": 名稱}、{"param": id}；可加 "adjPct"（數字或 "param:id"、"-param:id"）。"""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return x
    if not isinstance(x, dict):
        return None
    if "field" in x:
        v = field_value(c, x["field"])
    elif "param" in x:
        v = c.P.get(x["param"])
    else:
        v = x.get("value")
    if v is None:
        return None
    adj = x.get("adjPct")
    if adj is not None:
        if isinstance(adj, str):
            sign = -1 if adj.startswith("-") else 1
            adj = sign * c.P.get(adj.lstrip("-").removeprefix("param:"), 0)
        v = v * (1 + adj / 100)
    return v


def compare(c: Ctx, spec: dict) -> Res:
    a, b = operand(c, spec.get("left")), operand(c, spec.get("right"))
    if a is None or b is None:
        return Res(NA)
    ok = OPS[spec["op"]](a, b)
    txt = f"{spec.get('label', spec['id'])}（{fmt(a, 2)} {spec['op']} {fmt(b, 2)}）"
    return Res(PASS, txt) if ok else Res(FAIL, need=spec.get("need") or txt)
