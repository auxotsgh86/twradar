"""規則引擎：條件 → 組合 → 由上往下第一條成立的規則決定燈號。

組合語法（JSON）：
  "條件id"
  {"all": [...]}                 全部成立
  {"any": [...]}                 任一成立
  {"atLeast": {"n": 2, "of": [...]}}  至少 n 項成立
  {"none": [...]}                全部不成立
  {"not": x}

三值邏輯：成立 pass／不成立 fail／資料不足 na。
  all：有 fail → fail；否則有 na → na；否則 pass
  any：有 pass → pass；否則有 na → na；否則 fail
  atLeast n：pass 數 ≥ n → pass；pass＋na ≥ n → na；否則 fail
規則只有在結果為 pass 時才算符合；na 視為不符合，並記錄在判定軌跡中。
"""
from __future__ import annotations

import json

import copy

from .conditions import BUILTIN, NA, OPS, PASS, FAIL, COMPARE_FIELDS, Ctx, Res, compare, fmt

# 燈號：加碼面沿用 v0.1 的代碼（go/wait/stop），網頁與 API 不必改
ENTRY_LIGHTS = {"add": "go", "watch": "wait", "noAdd": "stop", "na": "na"}
HOLD_LIGHTS = ("keep", "reduce", "exit", "na")
LIGHT_LABEL = {"go": "加碼訊號", "wait": "觀察中", "stop": "不宜加碼", "na": "資料不足",
               "keep": "續抱", "reduce": "減碼", "exit": "退場"}
CHIP_CONDS = ("inst", "main", "retail")


RULES_VERSION = 6


def v03_playbooks() -> dict:
    """v0.3（2026-11-13 依專家審閱修改）。保留供升級比對與回溯對照。"""
    weak = ["whaleDown", "retailUp", "marginHigh", "instSellStreak"]
    return {
        "entry": [
            {"light": "na", "label": "資料不足", "when": "noData"},
            {"light": "noAdd", "label": "否決", "when": {"any": [
                "divergence", "marginDump", "marginExcess", "belowMa20", "blowoffBlack", "trustSellConflict"]}},
            {"light": "watch", "label": "大盤逆風", "when": "marketWeak"},
            {"light": "watch", "label": "投信買、外資賣", "when": "trustBuyConflict"},
            {"light": "add", "label": "加碼", "lightLabel": "加碼訊號",
             "when": {"all": ["isHolding", "addProfitOk", "pvBreakout", "instBuy"]}},
            {"light": "add", "label": "上車", "lightLabel": "上車訊號",
             "when": {"all": ["notHolding", "trendUp", "pullbackHeld", "redVolK", "aboveMa5And20",
                              {"any": ["instTurn", "instBuy"]}]}},
            {"light": "watch", "label": "觀察", "default": True},
        ],
        "hold": [
            {"light": "na", "label": "資料不足", "when": "noData"},
            {"light": "exit", "label": "停損", "confirm": 1, "when": {"any": [
                "stopLoss", "belowKeyLevel", "breakMa20Hard", {"all": ["belowMa20", "instNetSell"]}, "giveback"]}},
            {"light": "exit", "label": "獲利出場", "confirm": 1,
             "when": {"all": ["profitTarget", {"any": ["belowMa20", {"atLeast": {"n": 3, "of": weak}}]}]}},
            {"light": "reduce", "label": "出貨警訊", "confirm": 1,
             "when": {"any": ["highBlowoffBlack", "failedHigh", "limitUpOpenBlowoff", "biasTurn"]}},
            {"light": "reduce", "label": "獲利保護", "when": {"all": ["profitTarget", {"atLeast": {"n": 2, "of": weak}}]}},
            {"light": "keep", "label": "續抱", "default": True},
        ],
    }


def default_playbooks() -> dict:
    """預設規則 v0.6（2026-11-21，見 docs/STRATEGY.md 第 12 節）：v0.5 ＋ 外資撤出時不上車
    （外資現貨 5 日賣超、台指期淨空單 5 日增加、新台幣 5 日貶值，三項中成立 foreignExitSignals 項，預設 3 項都成立）。"""
    return veto_with("foreignExit")


def v05_playbooks() -> dict:
    """v0.5（2026-11-16，見 docs/STRATEGY.md 第 11 節）：v0.4 ＋ 大盤多頭時放寬月線停損
    （不因「跌破月線＋法人當日賣超」停損；跌破下彎月線超過 breakMaBullPct 才停損）。"""
    return bull_playbooks(stop=True)


def v04_playbooks() -> dict:
    """v0.4（2026-11-16，依回溯的規則對照與條件研究，見 docs/STRATEGY.md 第 10 節）。與 v0.3 的差別：
    - 法人條件改看 5 日累計（instBuy／trustBuyConflict → 5 日版）。
    - 否決只留「跌破月線」「融資追價過熱」；拿掉「外資買、投信賣」「籌碼背離」「法人賣、融資接」。
    - 「爆量長黑」從否決移到持有面的出貨警訊。"""
    pb = v03_playbooks()
    swap = {"instBuy": "inst5dBuy", "trustBuyConflict": "trustBuyConflict5d"}

    def sw(x):
        if isinstance(x, str):
            return swap.get(x, x)
        if isinstance(x, list):
            return [sw(y) for y in x]
        if isinstance(x, dict):
            return {k: sw(v) if k in ("all", "any", "none", "not", "when") else v for k, v in x.items()}
        return x
    entry = []
    for r in pb["entry"]:
        if r.get("label") == "否決":
            r = {**r, "when": {"any": ["belowMa20", "marginExcess"]}}
        elif not r.get("default"):
            r = {**r, "when": sw(r["when"])}
        entry.append(r)
    hold = []
    for r in pb["hold"]:
        if r.get("label") == "出貨警訊":
            r = {**r, "when": {"any": [*r["when"]["any"], "blowoffBlack"]}}
        hold.append(r)
    return {"entry": entry, "hold": hold}


# ---- 回溯對照用的候選規則（尚未採用） ----

def trial_playbooks(settled: bool = False) -> dict:
    """試單：月線還沒翻揚，但帶量紅 K 站上 5 日線與月線、法人 5 日買超、離 5 日線不遠 → 先進場（排在上車之後）。
    settled：試單另外要求「起漲前籌碼沉澱」（橫盤、融資低檔且未增加）。"""
    pb = default_playbooks()
    i = next(k for k, r in enumerate(pb["entry"]) if r.get("label") == "上車") + 1
    conds = ["notHolding", "redVolK", "aboveMa5And20", "inst5dBuy", "biasMa5Ok"] + (["baseSettled"] if settled else [])
    pb["entry"].insert(i, {"light": "add", "label": "試單", "lightLabel": "試單訊號", "when": {"all": conds}})
    return pb


def settled_playbooks(cond: str = "baseSettled") -> dict:
    """上車另外要求某個條件（預設「起漲前籌碼沉澱」）。"""
    pb = default_playbooks()
    for r in pb["entry"]:
        if r.get("label") == "上車":
            w = r["when"]
            r["when"] = {"all": [*(w["all"] if isinstance(w, dict) and "all" in w else [w]), cond]}
    return pb


def entry_with(*conds: str) -> dict:
    """以目前預設為底，上車另外要求 conds。"""
    pb = default_playbooks()
    for r in pb["entry"]:
        if r.get("label") == "上車":
            w = r["when"]
            r["when"] = {"all": [*(w["all"] if isinstance(w, dict) and "all" in w else [w]), *conds]}
    return pb


def stop_variant(kind: str) -> dict:
    """以目前預設為底，調整停損中和月線有關的部分。
    nosell：拿掉「跌破月線＋法人當日賣超」；two：月線相關的停損都要連 2 日收在月線下。"""
    pb = default_playbooks()

    def ma_related(x) -> bool:
        return "belowMa20" in json.dumps(x) or "breakMa20" in json.dumps(x)
    for r in pb["hold"]:
        if r.get("label") != "停損":
            continue
        xs = []
        for x in r["when"]["any"]:
            if kind == "nosell" and isinstance(x, dict) and "instNetSell" in json.dumps(x):
                continue
            if kind == "two" and ma_related(x):
                x = {"all": [x, "belowMa20Two"]}
            xs.append(x)
        r["when"] = {"any": xs}
    return pb


def veto_with(*conds: str) -> dict:
    """以 v0.5 為底，否決條件另外加上 conds（任一成立就不上車）。"""
    pb = v05_playbooks()
    for r in pb["entry"]:
        if r.get("label") == "否決":
            r["when"] = {"any": [*r["when"]["any"], *conds]}
    return pb


def trial_with(cond: str) -> dict:
    """試單另外要求某個條件。"""
    pb = trial_playbooks()
    for r in pb["entry"]:
        if r.get("label") == "試單":
            r["when"]["all"].append(cond)
    return pb


def bull_playbooks(hold: bool = False, stop: bool = False) -> dict:
    """以 v0.4 為底，依大盤環境調整出場。hold：大盤多頭時不因出貨警訊、獲利保護減碼；
    stop：大盤多頭時，跌破月線要超過較寬的幅度才停損，且不因「跌破月線＋法人當日賣超」停損（v0.5 採用）。"""
    pb = v04_playbooks()
    not_bull = "marketNotBull"   # 大盤資料不足時視為非多頭，停損照舊
    out = []
    for r in pb["hold"]:
        if hold and r.get("label") in ("出貨警訊", "獲利保護"):
            r = {**r, "when": {"all": [r["when"], not_bull]}}
        elif stop and r.get("label") == "停損":
            xs = []
            for x in r["when"]["any"]:
                if x == "breakMa20Hard":
                    x = {"any": [{"all": ["breakMa20Hard", not_bull]}, "breakMa20Deep"]}
                elif isinstance(x, dict) and x.get("all") == ["belowMa20", "instNetSell"]:
                    x = {"all": ["belowMa20", "instNetSell", not_bull]}
                xs.append(x)
            r = {**r, "when": {"any": xs}}
        out.append(r)
    pb["hold"] = out
    return pb


def p3_playbooks() -> dict:
    """v0.2 P3 的預設規則（加碼面與 v0.1.3 相同＋法人對作）。保留供升級比對與對照測試。"""
    weak = ["whaleDown", "retailUp", "marginHigh", "instSellStreak"]
    return {
        "entry": [
            {"light": "na", "label": "資料不足", "when": "noData"},
            {"light": "noAdd", "label": "否決", "when": {"any": ["divergence", "belowMa20", "blowoffBlack", "instConflict"]}},
            {"light": "add", "label": "加碼", "when": {"all": [{"atLeast": {"n": 2, "of": list(CHIP_CONDS)}, "label": "籌碼支持"}, "pvConfirm"]}},
            {"light": "watch", "label": "觀察", "default": True},
        ],
        "hold": [
            {"light": "na", "label": "資料不足", "when": "noData"},
            {"light": "exit", "label": "停損", "when": {"any": ["stopLoss", "belowKeyLevel", {"all": ["belowMa20", "instSellStreak"]}]}},
            {"light": "exit", "label": "獲利出場", "when": {"all": ["profitTarget", {"any": ["belowMa20", {"atLeast": {"n": 3, "of": weak}}]}]}},
            {"light": "reduce", "label": "獲利保護", "when": {"all": ["profitTarget", {"atLeast": {"n": 2, "of": weak}}]}},
            {"light": "reduce", "label": "過熱", "when": {"any": ["failedLimitUp", "limitUpOpenBlowoff", "ma20BiasHigh"]}},
            {"light": "keep", "label": "續抱", "default": True},
        ],
    }


# 規則引擎用到的參數（加在 settings 的內建參數清單）
ENGINE_PARAMS = [
    {"id": "conflictRatio", "label": "法人對作：較小一方 ≥ 較大一方的", "value": 25, "unit": "%", "min": 0, "max": 100, "note": "外資與投信一買一賣"},
    {"id": "conflictValuePct", "label": "法人對作：較小一方金額 ≥ 5 日均成交金額的", "value": 3, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "conflictMinAmt", "label": "法人對作：較小一方金額 ≥（無均額時）", "value": 3000, "unit": "萬元", "min": 0, "max": None, "note": "避免小量雜訊"},
    {"id": "amplifyVolPct", "label": "買超放大：當日買超佔成交量 ≥", "value": 10, "unit": "%", "min": 0, "max": 100, "note": "或金額達下一項"},
    {"id": "amplifyMinAmt", "label": "買超放大：當日買超金額 ≥", "value": 3000, "unit": "萬元", "min": 0, "max": None, "note": ""},
    {"id": "marginInstRatio", "label": "融資健康：融資增加 < 法人買超的", "value": 30, "unit": "%", "min": 0, "max": 1000, "note": ""},
    {"id": "inst5dPct", "label": "法人 5 日累計：佔近 5 日成交量 ≥", "value": 3, "unit": "%", "min": 0, "max": 100, "note": "5 日版的法人條件用"},
    {"id": "maxBiasMa5Pct", "label": "試單：收盤距 5 日線 ≤", "value": 5, "unit": "%", "min": 0, "max": 50, "note": "對照組「試單」用，不追高"},
    {"id": "breadthMinPct", "label": "市場廣度：站上月線的股票比例 <", "value": 40, "unit": "%", "min": 0, "max": 100, "note": "對照組「大盤環境」用"},
    {"id": "breadthDropPts", "label": "廣度背離：指數漲、廣度 10 日下降 ≥", "value": 10, "unit": "點", "min": 0, "max": 100, "note": ""},
    {"id": "foreignExitSignals", "label": "外資撤出：成立訊號數 ≥", "value": 3, "unit": "項", "min": 1, "max": 3, "note": "現貨賣超、期貨空單增加、新台幣貶值"},
    {"id": "foreignFutChgLots", "label": "外資撤出：台指期淨空單 5 日增加 ≥", "value": 2000, "unit": "口", "min": 0, "max": None, "note": ""},
    {"id": "foreignFxPct", "label": "外資撤出：新台幣 5 日貶值 ≥", "value": 0.3, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "maxBiasMa20Pct", "label": "不追高：收盤距月線 ≤", "value": 6, "unit": "%", "min": 0, "max": 50, "note": "對照組「大型股」用"},
    {"id": "ma20SlopePct", "label": "盤整不進場：月線 10 日上升 ≥", "value": 1, "unit": "%", "min": -10, "max": 20, "note": "對照組「大型股」用"},
    {"id": "baseRangePct", "label": "籌碼沉澱：起漲前整理區間高低差 ≤", "value": 25, "unit": "%", "min": 0, "max": 200, "note": "對照組「沉澱」用"},
    {"id": "baseMarginPos", "label": "籌碼沉澱：融資在近 60 日區間的位置 ≤", "value": 40, "unit": "%", "min": 0, "max": 100, "note": "0＝最低、100＝最高"},
    {"id": "whaleUpWeeksMin", "label": "籌碼集中：大戶比例連續上升 ≥", "value": 3, "unit": "週", "min": 1, "max": 8, "note": "對照組「集保」用"},
    {"id": "holdersDownWeeksMin", "label": "籌碼集中：股東人數連續下降 ≥", "value": 3, "unit": "週", "min": 0, "max": 8, "note": "0＝不看股東人數"},
    {"id": "breakMaBullPct", "label": "停損（大盤多頭時）：跌破下彎月線超過", "value": 3, "unit": "%", "min": 0, "max": 20, "note": "對照組「多頭放寬停損」用"},
    {"id": "addMinProfit", "label": "加碼前提：底倉獲利 ≥", "value": 3, "unit": "%", "min": 0, "max": 100, "note": "不攤平"},
    {"id": "pullbackDays", "label": "上車：回測觀察天數", "value": 10, "unit": "日", "min": 1, "max": 40, "note": ""},
    {"id": "pullbackPct", "label": "上車：回測時最低價距月線 ≤", "value": 2, "unit": "%", "min": -20, "max": 20, "note": "曾回到月線附近"},
    {"id": "pullbackBreakPct", "label": "上車：回測期間收盤跌破月線不超過", "value": 1.5, "unit": "%", "min": 0, "max": 20, "note": "有守"},
    {"id": "stopLossPct", "label": "停損：虧損 ≥", "value": 10, "unit": "%", "min": 0, "max": 100, "note": "最後保險；以成本計，含費稅"},
    {"id": "breakMaPct", "label": "停損：跌破下彎月線超過", "value": 1.5, "unit": "%", "min": 0, "max": 20, "note": ""},
    {"id": "givebackPeak", "label": "保本線：最高獲利曾達", "value": 10, "unit": "%", "min": 0, "max": 1000, "note": "需填成本與買進日"},
    {"id": "givebackFloor", "label": "保本線：回吐到獲利 ≤", "value": 2, "unit": "%", "min": -100, "max": 100, "note": ""},
    {"id": "profitTargetPct", "label": "獲利保護啟動：獲利 ≥", "value": 10, "unit": "%", "min": 0, "max": 1000, "note": ""},
    {"id": "sellStreakDays", "label": "法人合計連賣天數 ≥", "value": 2, "unit": "日", "min": 1, "max": 20, "note": ""},
    {"id": "marginUpDays", "label": "融資連增天數 ≥", "value": 2, "unit": "日", "min": 1, "max": 20, "note": ""},
    {"id": "marginHighPct", "label": "融資偏高：60 日區間位置 ≥", "value": 80, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "retailUpPct", "label": "散戶增加：股東人數週增 ≥", "value": 1, "unit": "%", "min": 0, "max": 100, "note": "集保總股東人數"},
    {"id": "marginRangeMin", "label": "融資偏高防呆：60 日融資變化 ≥", "value": 1000, "unit": "張", "min": 0, "max": None, "note": "或下一項"},
    {"id": "marginRangePct", "label": "融資偏高防呆：60 日融資變化 ≥ 股本的", "value": 1, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "limitNearPct", "label": "高檔長上影：盤中漲幅 ≥", "value": 7, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "failedVol", "label": "高檔長上影：量比 ≥", "value": 1.5, "unit": "倍", "min": 0, "max": 20, "note": ""},
    {"id": "limitTouchPct", "label": "觸及漲停：盤中漲幅 ≥", "value": 9.5, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "shadowPct", "label": "長上影：上影線佔振幅 ≥", "value": 50, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "openBlowoff", "label": "爆量開板：量比 ≥", "value": 2, "unit": "倍", "min": 1, "max": 20, "note": ""},
    {"id": "biasHighPct", "label": "月線乖離過大 ≥", "value": 15, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "biasHighLargePct", "label": "月線乖離過大（大型股）≥", "value": 10, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "largeCapAmt", "label": "大型股：市值 ≥", "value": 1000, "unit": "億元", "min": 0, "max": None, "note": "以集保總股數 × 收盤估算"},
    {"id": "highZonePct", "label": "高檔：昨收距近 20 日最高 ≤", "value": 5, "unit": "%", "min": 0, "max": 50, "note": ""},
    {"id": "highBlackPct", "label": "高檔爆量長黑：跌幅 ≥", "value": 3, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "highBlackVol", "label": "高檔爆量長黑：量比 ≥", "value": 1.8, "unit": "倍", "min": 0, "max": 20, "note": ""},
    {"id": "whaleLots", "label": "大戶門檻（固定張數）：持股 ≥", "value": 1000, "unit": "張", "min": 1, "max": 1000,
     "note": "市值門檻設 0 時使用；個股可在觀察清單另設"},
    {"id": "whaleAmount", "label": "大戶門檻：持股市值 ≥", "value": 10000, "unit": "萬元", "min": 0, "max": None,
     "note": "依 60 日均價換算張數、對到最接近的集保分級；0 = 改用固定張數；個股另設的張數優先"},
    {"id": "retailMaxLots", "label": "散戶：持股 ≤", "value": 50, "unit": "張", "min": 1, "max": 1000, "note": "取最接近的集保分級"},
    {"id": "whaleDropPts", "label": "大戶減碼：持股比例週減 ≥", "value": 0, "unit": "百分點", "min": 0, "max": 100,
     "note": "0 = 只要減少就算"},
    {"id": "feePct", "label": "手續費率", "value": 0.1425, "unit": "%", "min": 0, "max": 1, "note": "含折扣請自行調整"},
    {"id": "taxPct", "label": "證交稅率", "value": 0.3, "unit": "%", "min": 0, "max": 1, "note": "ETF 為 0.1"},
]


# v0.2 → v0.3 改變的參數預設值：使用者沒改過（仍是舊預設）才更新
PARAM_DEFAULT_CHANGES = {"profitTargetPct": (20, 10), "limitNearPct": (9, 7), "whaleAmount": (0, 10000),
                         "conflictMinAmt": (1000, 3000)}


def upgrade(s: dict) -> dict:
    """設定升級到 RULES_VERSION：舊預設規則換成新預設、沒改過的參數換成新預設值。使用者自訂的規則與參數不動。"""
    v = s.get("rulesVersion", 0)
    if v >= RULES_VERSION:
        return s
    pb = s.get("playbooks")
    if isinstance(pb, dict):
        olds = [v03_playbooks(), v04_playbooks(), v05_playbooks()] + ([p3_playbooks()] if v < 3 else [])
        for k in ("entry", "hold"):
            if not pb.get(k) or any(pb.get(k) == old[k] for old in olds):
                pb[k] = default_playbooks()[k]
    if v < 3:
        for p in s.get("params", []):
            if isinstance(p, dict) and p.get("id") in PARAM_DEFAULT_CHANGES and p.get("value") == PARAM_DEFAULT_CHANGES[p["id"]][0]:
                p["value"] = PARAM_DEFAULT_CHANGES[p["id"]][1]
    s["rulesVersion"] = RULES_VERSION
    return s


# ---------------- 求值 ----------------

class Engine:
    def __init__(self, custom: list[dict]):
        self.custom = {c["id"]: c for c in custom}

    def leaf(self, cid: str, ctx: Ctx) -> Res:
        if cid in ctx.cache:
            return ctx.cache[cid]
        if cid in BUILTIN:
            r = BUILTIN[cid].fn(ctx)
        elif cid in self.custom:
            r = compare(ctx, self.custom[cid])
        else:
            r = Res(NA)
        ctx.cache[cid] = r
        return r

    def label(self, cid: str) -> str:
        if cid in BUILTIN:
            return BUILTIN[cid].label
        return self.custom.get(cid, {}).get("label", cid)

    def ev(self, node, ctx: Ctx) -> str:
        if isinstance(node, str):
            return self.leaf(node, ctx).state
        if "all" in node:
            s = [self.ev(x, ctx) for x in node["all"]]
            return FAIL if FAIL in s else NA if NA in s else PASS
        if "any" in node:
            s = [self.ev(x, ctx) for x in node["any"]]
            return PASS if PASS in s else NA if NA in s else FAIL
        if "none" in node:
            s = [self.ev(x, ctx) for x in node["none"]]
            return FAIL if PASS in s else NA if NA in s else PASS
        if "not" in node:
            s = self.ev(node["not"], ctx)
            return {PASS: FAIL, FAIL: PASS}.get(s, NA)
        if "atLeast" in node:
            n, of = node["atLeast"]["n"], node["atLeast"]["of"]
            s = [self.ev(x, ctx) for x in of]
            p, na = s.count(PASS), s.count(NA)
            return PASS if p >= n else NA if p + na >= n else FAIL
        raise ValueError(f"無法辨識的組合：{node}")

    # ---- 說明 ----
    def reasons(self, node, ctx: Ctx) -> list[str]:
        """成立的規則：列出成立的條件說明。"""
        if isinstance(node, str):
            r = self.leaf(node, ctx)
            return [r.text or self.label(node)] if r.state == PASS else []
        out: list[str] = []
        for x in _children(node):
            if self.ev(x, ctx) == PASS:
                out += self.reasons(x, ctx)
        return out

    def needs(self, node, ctx: Ctx, siblings_pass: bool = False, summary: bool = True) -> list[str]:
        """列出還差什麼：所有不成立的條件；至少 n 項不足、且同組其他條件都成立時，加一句「再多 k 項」。"""
        if isinstance(node, str):
            r = self.leaf(node, ctx)
            return [r.need] if r.state == FAIL and r.need else []
        if "all" in node:
            kids = node["all"]
            st = [self.ev(x, ctx) for x in kids]
            out = []
            for i, x in enumerate(kids):
                others = all(s == PASS for j, s in enumerate(st) if j != i)
                out += self.needs(x, ctx, others, summary)
            return out
        if "atLeast" in node:
            n, of = node["atLeast"]["n"], node["atLeast"]["of"]
            out = []
            for x in of:
                out += self.needs(x, ctx, False, summary)
            p = [self.ev(x, ctx) for x in of].count(PASS)
            if summary and p < n and siblings_pass:
                out.append(f"再多 {n - p} 項{node.get('label', '條件')}")
            return out
        if "any" in node:
            for x in node["any"]:
                got = self.needs(x, ctx, False, summary)
                if got:
                    return got[:1]
        return []

    def run(self, playbook: list[dict], ctx: Ctx) -> dict:
        for rule in playbook:
            if rule.get("default") or self.ev(rule["when"], ctx) == PASS:
                return {"light": rule["light"], "rule": rule.get("label", rule["light"]), "lightLabel": rule.get("lightLabel"),
                        "confirm": rule.get("confirm"),
                        "reasons": [] if rule.get("default") else self.reasons(rule["when"], ctx)}
        return {"light": "na", "rule": "", "reasons": [], "lightLabel": None, "confirm": None}

    def trace(self, ctx: Ctx) -> list[dict]:
        return [{"id": k, "label": self.label(k), "state": r.state, "text": r.text or r.need}
                for k, r in ctx.cache.items()]


def _children(node) -> list:
    if isinstance(node, str):
        return []
    for k in ("all", "any", "none"):
        if k in node:
            return node[k]
    if "atLeast" in node:
        return node["atLeast"]["of"]
    if "not" in node:
        return [node["not"]]
    return []


def refs(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    out = []
    for x in _children(node):
        out += refs(x)
    return out


# ---------------- 判定（輸出格式與 v0.1 相容，另加 trace、hold） ----------------

def missing_of(rec: dict) -> list[str]:
    g = rec.get
    out = []
    if g("foreign") is None and g("trust") is None:
        out.append("三大法人")
    if g("mainForce") is None and g("brokerDiff") is None:
        out.append("主力分點")
    if g("marginChg") is None or g("chgPct") is None:
        out.append("融資")
    has_pv = g("close") is not None and g("ma20") is not None
    vr = g("vol") / g("vol5") if g("vol") is not None and g("vol5") else None
    if not has_pv:
        out.append("收盤/月線")
    elif vr is None:
        out.append("量能")
    return out


def evaluate(rec: dict, stock: dict, P: dict, settings: dict | None = None) -> dict:
    """單檔判定。settings 為完整設定（取 playbooks、conditions）；None 時用預設規則。"""
    pb = (settings or {}).get("playbooks") or default_playbooks()
    eng = Engine((settings or {}).get("conditions") or [])
    ctx = Ctx(rec, stock, P)
    entry_pb = pb.get("entry") or default_playbooks()["entry"]
    entry = eng.run(entry_pb, ctx)
    light = ENTRY_LIGHTS.get(entry["light"], entry["light"])
    # 顯示用的四項檢查：新規則用 v0.3 條件，舊規則用 v0.1 條件
    used = {x for r in entry_pb if not r.get("default") for x in refs(r["when"])}
    v3 = "instBuy" in used or "inst5dBuy" in used or "pvTrend" in used or "pvBreakout" in used
    ck_ids = {"inst": "inst5dBuy" if "inst5dBuy" in used else "instBuy" if v3 else "inst", "main": "main", "retail": "retailHealthy" if v3 else "retail",
              "priceVolume": "pvTrend" if v3 else "pvConfirm"}

    # 與 v0.1 相同的欄位
    if light == "stop":
        warns = entry["reasons"]
    else:  # 其他燈號也列出已成立的否決條件（例如資料不足但已爆量長黑）
        veto = [r for r in (pb.get("entry") or []) if r.get("light") == "noAdd" and not r.get("default")]
        warns = eng.reasons(veto[0]["when"], ctx) if veto else []
    needs: list[str] = []
    gates = ("isHolding", "notHolding")
    add_rules = [r for r in entry_pb if r.get("light") == "add" and not r.get("default")
                 and not any(eng.leaf(gid, ctx).state == FAIL for gid in refs(r["when"]) if gid in gates)]
    if add_rules:
        needs = [n for n in eng.needs(add_rules[0]["when"], ctx, summary=(light == "wait")) if not n.startswith("（")]
    chk = {k: eng.leaf(cid, ctx).state for k, cid in ck_ids.items()}
    chips = sum(1 for k in ("inst", "main", "retail") if chk[k] == PASS)
    close, ma20 = rec.get("close"), rec.get("ma20")
    has_pv = close is not None and ma20 is not None
    vr = ctx.vol_r
    below = eng.leaf("belowMa20", ctx).state == PASS if has_pv else None
    key_note = None
    kl = stock.get("keyLevel")
    if kl is not None and close is not None:
        above = close >= kl
        key_note = f"關鍵價 {fmt(kl)}：" + ("已站上" if above else f"尚未站上（差 {fmt(kl - close, 2)}）")
        if above and vr is not None:
            key_note += "，量能確認" if vr > P["volRatio"] else "，但量能不足"
    out = {"light": light, "lightLabel": entry["lightLabel"] or LIGHT_LABEL.get(light, light),
           "checks": chk, "instWhy": eng.leaf(ck_ids["inst"], ctx).text, "chipsPassed": chips, "needs": needs, "warnings": warns,
           "missing": missing_of(rec), "volRatio": None if vr is None else round(vr, 3),
           "maDistPct": None if not has_pv else round((close / ma20 - 1) * 100, 2),
           "belowMa20": below, "keyLevelNote": key_note, "rule": entry["rule"], "confirm": entry["confirm"],
           "entryReasons": entry["reasons"]}
    if light == "wait" and entry["rule"] not in ("觀察", "") and entry["reasons"]:
        out["warnings"] = out["warnings"] + [x for x in entry["reasons"] if x not in out["warnings"]]
    if stock.get("holding"):
        h = eng.run(pb.get("hold") or default_playbooks()["hold"], ctx)
        prof = ctx.profit_pct
        warn = [eng.leaf(k, ctx).text for k in ("whaleDown", "retailUp", "marginHigh", "instSellStreak", "belowMa20")
                if eng.leaf(k, ctx).state == PASS]
        out["hold"] = {"light": h["light"], "lightLabel": h["lightLabel"] or LIGHT_LABEL.get(h["light"], h["light"]),
                       "rule": h["rule"], "confirm": h["confirm"],
                       "reasons": h["reasons"], "weak": [w for w in warn if w not in h["reasons"]],
                       "profitPct": None if prof is None else round(prof, 2)}
        if rec.get("costExDates") and ctx.cost:
            out["hold"]["costAdj"] = round(ctx.cost, 2)
            out["hold"]["costExDates"] = rec["costExDates"]
    out["trace"] = eng.trace(ctx)
    return out


# ---------------- 驗證（settings.validate 呼叫） ----------------

def validate_rules(s: dict, e) -> None:
    """檢查 conditions 與 playbooks；錯誤以 e(path, message) 回報。"""
    params = {p.get("id") for p in s.get("params", []) if isinstance(p, dict)}
    custom: set[str] = set()
    conds = s.get("conditions")
    if isinstance(conds, list):
        for i, c in enumerate(conds):
            p = f"conditions[{i}]"
            if not isinstance(c, dict):
                e(p, "必須是物件"); continue
            cid = c.get("id")
            if not isinstance(cid, str) or not cid.isidentifier() or len(cid) > 40:
                e(f"{p}.id", "必須是英文字母開頭的識別名稱")
            elif cid in BUILTIN:
                e(f"{p}.id", f"{cid} 是內建條件，請換一個名稱")
            elif cid in custom:
                e(f"{p}.id", f"{cid} 重複")
            custom.add(cid)
            if not isinstance(c.get("label"), str) or not c["label"].strip():
                e(f"{p}.label", "名稱不可空白")
            if c.get("op") not in OPS:
                e(f"{p}.op", f"必須是 {' '.join(OPS)} 之一")
            for side in ("left", "right"):
                _check_operand(c.get(side), f"{p}.{side}", params, e)
            for k in c:
                if k not in ("id", "label", "left", "op", "right", "need", "note"):
                    e(f"{p}.{k}", "未知的欄位")
    known = set(BUILTIN) | custom
    pb = s.get("playbooks")
    if not isinstance(pb, dict):
        return
    for side, lights in (("entry", set(ENTRY_LIGHTS)), ("hold", set(HOLD_LIGHTS))):
        rules = pb.get(side)
        if not isinstance(rules, list):
            continue
        if not rules:
            e(f"playbooks.{side}", "至少要有一條預設規則")
            continue
        for i, r in enumerate(rules):
            p = f"playbooks.{side}[{i}]"
            if not isinstance(r, dict):
                e(p, "必須是物件"); continue
            if r.get("light") not in lights:
                e(f"{p}.light", f"必須是 {', '.join(sorted(lights))} 之一")
            if r.get("default"):
                if i != len(rules) - 1:
                    e(p, "預設規則必須放在最後一條")
            elif "when" not in r:
                e(f"{p}.when", "缺少條件")
            else:
                _check_node(r["when"], f"{p}.when", known, e)
            if "confirm" in r and (not isinstance(r["confirm"], int) or isinstance(r["confirm"], bool) or not 1 <= r["confirm"] <= 5):
                e(f"{p}.confirm", "必須是 1～5 的整數")
            if "lightLabel" in r and not isinstance(r["lightLabel"], str):
                e(f"{p}.lightLabel", "必須是文字")
            for k in r:
                if k not in ("light", "label", "lightLabel", "when", "default", "note", "confirm"):
                    e(f"{p}.{k}", "未知的欄位")
        if not rules[-1].get("default"):
            e(f"playbooks.{side}", "最後一條必須是預設規則（\"default\": true）")
    for k in pb:
        if k not in ("entry", "hold"):
            e(f"playbooks.{k}", "未知的規則組")


def _check_operand(x, path, params, e):
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return
    if not isinstance(x, dict):
        e(path, "必須是數字、{\"field\": 欄位} 或 {\"param\": 參數}"); return
    if "field" in x:
        if x["field"] not in COMPARE_FIELDS:
            e(f"{path}.field", f"未知的欄位 {x['field']}")
    elif "param" in x:
        if x["param"] not in params:
            e(f"{path}.param", f"未知的參數 {x['param']}")
    elif not isinstance(x.get("value"), (int, float)):
        e(path, "必須指定 field、param 或 value")
    adj = x.get("adjPct")
    if adj is not None:
        if isinstance(adj, str):
            if adj.lstrip("-").removeprefix("param:") not in params:
                e(f"{path}.adjPct", f"未知的參數 {adj}")
        elif not isinstance(adj, (int, float)) or isinstance(adj, bool):
            e(f"{path}.adjPct", "必須是數字或 \"param:參數\"")


def _check_node(node, path, known, e, depth=0):
    if depth > 8:
        e(path, "組合層數太深（最多 8 層）"); return
    if isinstance(node, str):
        if node not in known:
            e(path, f"未知的條件 {node}")
        return
    if not isinstance(node, dict):
        e(path, "必須是條件 id 或組合物件"); return
    ops = [k for k in ("all", "any", "none", "not", "atLeast") if k in node]
    if len(ops) != 1:
        e(path, "必須恰好包含 all、any、none、not、atLeast 其中一種"); return
    op = ops[0]
    if op in ("all", "any", "none"):
        if not isinstance(node[op], list) or not node[op]:
            e(f"{path}.{op}", "必須是非空陣列"); return
        for i, x in enumerate(node[op]):
            _check_node(x, f"{path}.{op}[{i}]", known, e, depth + 1)
    elif op == "not":
        _check_node(node["not"], f"{path}.not", known, e, depth + 1)
    else:
        a = node["atLeast"]
        if not isinstance(a, dict) or not isinstance(a.get("of"), list) or not a["of"]:
            e(f"{path}.atLeast", "必須是 {\"n\": 數字, \"of\": [...]}"); return
        n = a.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(a["of"]):
            e(f"{path}.atLeast.n", f"必須是 1～{len(a['of'])} 的整數")
        for i, x in enumerate(a["of"]):
            _check_node(x, f"{path}.atLeast.of[{i}]", known, e, depth + 1)
    for k in node:
        if k not in (op, "label"):
            e(f"{path}.{k}", "未知的欄位")


def catalog(settings: dict) -> dict:
    """設定頁用：可用的條件、欄位、運算子。"""
    return {"builtin": [{"id": c.id, "label": c.label, "side": c.side, "params": list(c.params), "note": c.note}
                        for c in BUILTIN.values()],
            "custom": copy.deepcopy(settings.get("conditions", [])),
            "fields": COMPARE_FIELDS, "ops": list(OPS),
            "lights": {"entry": list(ENTRY_LIGHTS), "hold": list(HOLD_LIGHTS)}}
