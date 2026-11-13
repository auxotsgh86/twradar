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

import copy

from .conditions import BUILTIN, NA, OPS, PASS, FAIL, COMPARE_FIELDS, Ctx, Res, compare, fmt

# 燈號：加碼面沿用 v0.1 的代碼（go/wait/stop），網頁與 API 不必改
ENTRY_LIGHTS = {"add": "go", "watch": "wait", "noAdd": "stop", "na": "na"}
HOLD_LIGHTS = ("keep", "reduce", "exit", "na")
LIGHT_LABEL = {"go": "加碼訊號", "wait": "觀察中", "stop": "不宜加碼", "na": "資料不足",
               "keep": "續抱", "reduce": "減碼", "exit": "退場"}
CHIP_CONDS = ("inst", "main", "retail")


def default_playbooks() -> dict:
    """預設規則。加碼面與 v0.1.3 結果相同，另加「法人對作」；持有面依 STRATEGY.md 第 5 節。"""
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
    {"id": "conflictMinAmt", "label": "法人對作：較小一方金額 ≥", "value": 1000, "unit": "萬元", "min": 0, "max": None, "note": "避免小量雜訊"},
    {"id": "stopLossPct", "label": "停損：虧損 ≥", "value": 10, "unit": "%", "min": 0, "max": 100, "note": "以成本計，含費稅"},
    {"id": "profitTargetPct", "label": "獲利保護啟動：獲利 ≥", "value": 20, "unit": "%", "min": 0, "max": 1000, "note": ""},
    {"id": "sellStreakDays", "label": "法人合計連賣天數 ≥", "value": 2, "unit": "日", "min": 1, "max": 20, "note": ""},
    {"id": "marginUpDays", "label": "融資連增天數 ≥", "value": 2, "unit": "日", "min": 1, "max": 20, "note": ""},
    {"id": "marginHighPct", "label": "融資偏高：60 日區間位置 ≥", "value": 80, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "limitNearPct", "label": "接近漲停：盤中漲幅 ≥", "value": 9, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "limitTouchPct", "label": "觸及漲停：盤中漲幅 ≥", "value": 9.5, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "shadowPct", "label": "長上影：上影線佔振幅 ≥", "value": 50, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "openBlowoff", "label": "爆量開板：量比 ≥", "value": 2, "unit": "倍", "min": 1, "max": 20, "note": ""},
    {"id": "biasHighPct", "label": "月線乖離過大 ≥", "value": 15, "unit": "%", "min": 0, "max": 100, "note": ""},
    {"id": "whaleLots", "label": "大戶門檻：持股 ≥", "value": 1000, "unit": "張", "min": 1, "max": 1000,
     "note": "千張大戶；個股可在觀察清單另設。取最接近的集保分級"},
    {"id": "whaleAmount", "label": "大戶門檻改用市值：持股 ≥", "value": 0, "unit": "萬元", "min": 0, "max": None,
     "note": "0 = 不使用；設 10000（1 億）則依 60 日均價換算張數，個股另設的張數優先"},
    {"id": "retailMaxLots", "label": "散戶：持股 ≤", "value": 50, "unit": "張", "min": 1, "max": 1000, "note": "取最接近的集保分級"},
    {"id": "whaleDropPts", "label": "大戶減碼：持股比例週減 ≥", "value": 0, "unit": "百分點", "min": 0, "max": 100,
     "note": "0 = 只要減少就算"},
    {"id": "feePct", "label": "手續費率", "value": 0.1425, "unit": "%", "min": 0, "max": 1, "note": "含折扣請自行調整"},
    {"id": "taxPct", "label": "證交稅率", "value": 0.3, "unit": "%", "min": 0, "max": 1, "note": "ETF 為 0.1"},
]


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
                return {"light": rule["light"], "rule": rule.get("label", rule["light"]),
                        "reasons": [] if rule.get("default") else self.reasons(rule["when"], ctx)}
        return {"light": "na", "rule": "", "reasons": []}

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
    entry = eng.run(pb.get("entry") or default_playbooks()["entry"], ctx)
    light = ENTRY_LIGHTS.get(entry["light"], entry["light"])

    # 與 v0.1 相同的欄位
    if light == "stop":
        warns = entry["reasons"]
    else:  # 其他燈號也列出已成立的否決條件（例如資料不足但已爆量長黑）
        veto = [r for r in (pb.get("entry") or []) if r.get("light") == "noAdd" and not r.get("default")]
        warns = eng.reasons(veto[0]["when"], ctx) if veto else []
    needs: list[str] = []
    add_rules = [r for r in (pb.get("entry") or []) if r.get("light") == "add" and not r.get("default")]
    if add_rules:
        needs = eng.needs(add_rules[0]["when"], ctx, summary=(light == "wait"))
    chk = {k: eng.leaf(k, ctx).state for k in (*CHIP_CONDS, "pvConfirm")}
    chips = sum(1 for k in CHIP_CONDS if chk[k] == PASS)
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
    out = {"light": light, "lightLabel": LIGHT_LABEL.get(light, light),
           "checks": {"inst": chk["inst"], "main": chk["main"], "retail": chk["retail"], "priceVolume": chk["pvConfirm"]},
           "instWhy": eng.leaf("inst", ctx).text, "chipsPassed": chips, "needs": needs, "warnings": warns,
           "missing": missing_of(rec), "volRatio": None if vr is None else round(vr, 3),
           "maDistPct": None if not has_pv else round((close / ma20 - 1) * 100, 2),
           "belowMa20": below, "keyLevelNote": key_note, "rule": entry["rule"]}
    if stock.get("holding"):
        h = eng.run(pb.get("hold") or default_playbooks()["hold"], ctx)
        prof = ctx.profit_pct
        warn = [eng.leaf(k, ctx).text for k in ("whaleDown", "retailUp", "marginHigh", "instSellStreak", "belowMa20")
                if eng.leaf(k, ctx).state == PASS]
        out["hold"] = {"light": h["light"], "lightLabel": LIGHT_LABEL.get(h["light"], h["light"]), "rule": h["rule"],
                       "reasons": h["reasons"], "weak": [w for w in warn if w not in h["reasons"]],
                       "profitPct": None if prof is None else round(prof, 2)}
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
            for k in r:
                if k not in ("light", "label", "when", "default", "note"):
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
