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


@condition("retailUp", "散戶增加", "hold", ("marginUpDays",), ("retailChg", "marginStreak"),
           "散戶持股人數週增（集保），或融資連續增加")
def retail_up(c: Ctx) -> Res:
    r, ms = c.g("retailChg"), c.g("marginStreak")
    if r is None and ms is None:
        return Res(NA)
    if r is not None and r > 0:
        return Res(PASS, f"{fmt(c.g('retailLots'))} 張以下散戶人數 +{r:,}")
    if ms is not None and ms >= c.P["marginUpDays"]:
        return Res(PASS, f"融資連 {ms} 日增加")
    return Res(FAIL)


@condition("marginHigh", "融資偏高", "hold", ("marginHighPct",), ("marginPos60",),
           "融資餘額位於近 60 日區間的高檔")
def margin_high(c: Ctx) -> Res:
    pos = c.g("marginPos60")
    if pos is None:
        return Res(NA)
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


# ---------------- 比較條件（網頁新增） ----------------

COMPARE_FIELDS = {
    "close": "收盤", "open": "開盤", "high": "最高", "low": "最低", "chgPct": "漲跌幅%", "vol": "成交量（張）",
    "vol5": "5 日均量（張）", "volRatio": "量比", "ma20": "月線", "ma60": "季線", "prevHigh": "前高",
    "maDistPct": "月線乖離%", "highPct": "盤中最高漲幅%", "upperShadowPct": "上影線佔振幅%",
    "foreign": "外資買賣超（張）", "trust": "投信買賣超（張）", "dealer": "自營商買賣超（張）",
    "foreignStreak": "外資連買（負為連賣）", "trustStreak": "投信連買（負為連賣）", "instStreak": "法人合計連買（負為連賣）",
    "foreign5d": "外資 5 日合計（張）", "trust5d": "投信 5 日合計（張）",
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
