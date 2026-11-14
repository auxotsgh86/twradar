"""標籤：觀察清單的分類，以及「策略」標籤帶的規則覆寫。

- 一般標籤（例如 AI伺服器、散熱）：只用來分類與篩選，一檔可以貼很多個。
- 範圍標籤 `範圍::名稱`（同 GitLab scoped label）：同一個範圍一檔只能有一個，後貼的取代先貼的。
- `策略::名稱` 可以帶規則：只寫與全域預設不同的參數（params），以及是否允許加碼（allowAdd）。
  個股不放規則；規則一定是一群股票共用，才能用回溯分析與模型追蹤驗證。
"""
from __future__ import annotations

import re

SEP = "::"
STRATEGY = "策略"
PALETTE = ("#1f75cb", "#108548", "#ab6100", "#dd2b0e", "#6666c4", "#0e7a8a", "#a3478f", "#5c6b7a")
NAME_RE = re.compile(r"[^,\n\r\t]{1,30}")
# 會改變指標計算的參數：不能逐檔覆寫（指標是整批算的）
INDICATOR_PARAMS = ("pullbackDays", "whaleLots", "whaleAmount", "retailMaxLots")


def scope_of(name: str) -> str | None:
    return name.split(SEP, 1)[0] if SEP in name else None


def is_strategy(name: str) -> bool:
    return scope_of(name) == STRATEGY


def color_for(name: str) -> str:
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def clean(labels) -> list[str]:
    """去空白、去重複；同一個範圍只留最後貼的那個。順序維持第一次出現的位置。"""
    out: list[str] = []
    for x in labels or []:
        if not isinstance(x, str) or not x.strip():
            continue
        x = x.strip()
        sc = scope_of(x)
        if sc is not None:
            out = [y for y in out if scope_of(y) != sc]
        if x not in out:
            out.append(x)
    return out


def new_label(name: str, **kw) -> dict:
    base = {"name": name, "color": color_for(name), "params": {}, "allowAdd": True}
    base.update({k: v for k, v in kw.items() if k in base and v is not None})
    return base


def group_of(stock: dict) -> str:
    """相容舊欄位 group：第一個一般標籤。"""
    return next((x for x in stock.get("labels") or [] if scope_of(x) is None), "")


def strategy_of(settings: dict, stock: dict) -> dict | None:
    name = next((x for x in stock.get("labels") or [] if is_strategy(x)), None)
    if not name:
        return None
    return next((lb for lb in settings.get("labels") or [] if lb.get("name") == name), None)


def resolve(settings: dict, stock: dict, P: dict) -> tuple[dict, dict, str | None]:
    """這檔股票實際套用的（參數、設定、策略名稱）：策略標籤 > 全域預設。"""
    lb = strategy_of(settings, stock)
    if not lb:
        return P, settings, None
    P2 = {**P, **{k: v for k, v in (lb.get("params") or {}).items() if k in P}}
    S2 = settings
    if lb.get("allowAdd") is False:
        from .rules import refs
        pb = dict(settings.get("playbooks") or {})
        pb["entry"] = [r for r in pb.get("entry") or []
                       if not (r.get("light") == "add" and not r.get("default") and "isHolding" in refs(r["when"]))]
        S2 = {**settings, "playbooks": pb}
    return P2, S2, lb["name"]


def validate_labels(s: dict, e) -> None:
    labs = s.get("labels")
    if not isinstance(labs, list):
        e("labels", "必須是陣列")
        return
    params = {p.get("id"): p for p in s.get("params", []) if isinstance(p, dict)}
    seen = set()
    for i, lb in enumerate(labs):
        p = f"labels[{i}]"
        if not isinstance(lb, dict):
            e(p, "必須是物件")
            continue
        name = lb.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name != name.strip() or name.endswith(SEP) or name.startswith(SEP):
            e(f"{p}.name", "標籤名稱必須是 1～30 個字，不可含逗號；範圍標籤寫成「範圍::名稱」")
            continue
        if name in seen:
            e(f"{p}.name", f"{name} 重複")
        seen.add(name)
        if not isinstance(lb.get("color"), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", lb["color"]):
            e(f"{p}.color", "顏色必須是 #RRGGBB")
        if not isinstance(lb.get("allowAdd", True), bool):
            e(f"{p}.allowAdd", "必須是 true 或 false")
        ov = lb.get("params", {})
        if not isinstance(ov, dict):
            e(f"{p}.params", "必須是物件")
            continue
        if (ov or lb.get("allowAdd") is False) and not is_strategy(name):
            e(p, f"只有「{STRATEGY}{SEP}名稱」的標籤可以帶規則")
        for k, v in ov.items():
            prm = params.get(k)
            if prm is None:
                e(f"{p}.params.{k}", "未知的參數")
            elif k in INDICATOR_PARAMS:
                e(f"{p}.params.{k}", "這個參數會改變指標計算，只能在全域設定")
            elif isinstance(v, bool) or not isinstance(v, (int, float)):
                e(f"{p}.params.{k}", "必須是數字")
            elif (prm.get("min") is not None and v < prm["min"]) or (prm.get("max") is not None and v > prm["max"]):
                e(f"{p}.params.{k}", f"必須在 {prm.get('min')}～{prm.get('max')} 之間")
        for k in lb:
            if k not in ("name", "color", "params", "allowAdd"):
                e(f"{p}.{k}", "未知的欄位")
    for i, w in enumerate(s.get("watchlist") or []):
        if not isinstance(w, dict):
            continue
        ls = w.get("labels")
        if not isinstance(ls, list) or any(not isinstance(x, str) for x in ls):
            e(f"watchlist[{i}].labels", "必須是文字陣列")
            continue
        if clean(ls) != ls:
            e(f"watchlist[{i}].labels", "標籤重複，或同一個範圍（例如 策略::）貼了兩個")
        for x in ls:
            if x not in seen:
                e(f"watchlist[{i}].labels", f"標籤「{x}」不存在")
