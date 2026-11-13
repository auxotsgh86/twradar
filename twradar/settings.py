"""使用者設定：settings.json（觀察清單、參數、規則、資料源、排程、顏色）。

- 存檔前驗證；失敗不寫入，回報每個錯誤的位置（例如 watchlist[3].code）。
- 原子寫入（暫存檔再改名）；每次存檔把舊版放到 settings.history/，保留 20 版。
- 檔案被手動修改時自動重新載入；內容不合法就沿用上一個合法版本並記錄錯誤。
- 第一次啟動時從 v0.1.x 的 SQLite（觀察清單、門檻）與 config.json（排程、資料源）遷移。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("twradar.settings")

SCHEMA_VERSION = 1
HISTORY_KEEP = 20
SENSITIVE_KEYS = ("token", "secret", "password", "apikey", "api_key")
MASK = "••••"

SEED_WATCHLIST = [  # 首次安裝的預設觀察清單
    ("2330", "台積電", "TWSE", "科技／半導體權值", None, "已建 100 股底倉；看月線回踩是否守住"),
    ("2454", "聯發科", "TWSE", "科技／半導體權值", 4000, "已建 100 股底倉；看是否帶量站穩 4,000 月線反壓區"),
    ("2303", "聯電", "TWSE", "科技／半導體權值", None, "成熟製程晶圓代工"),
    ("6488", "環球晶", "TPEx", "科技／半導體權值", None, "矽晶圓強勢反彈代表"),
    ("6669", "緯穎", "TWSE", "AI 硬體／伺服器", None, "AI 伺服器"),
    ("3081", "聯亞", "TPEx", "AI 硬體／矽光子", None, "矽光子"),
    ("3163", "波若威", "TPEx", "AI 硬體／網通", None, "網通光通訊"),
    ("3017", "奇鋐", "TWSE", "AI 硬體／散熱", None, "看外資是否由賣轉買、連續性"),
    ("8358", "金居", "TPEx", "AI 硬體／CCL", None, "CCL 銅箔基板；看主力分點是否持續鎖碼"),
    ("2383", "台光電", "TWSE", "AI 硬體／CCL", None, "高階 CCL"),
    ("3443", "創意", "TWSE", "AI 硬體／ASIC", None, "ASIC／矽智財"),
    ("2881", "富邦金", "TWSE", "金融權值防禦", None, ""),
    ("2885", "元大金", "TWSE", "金融權值防禦", None, ""),
]

# 內建參數（判定門檻）。使用者可修改數值、可新增自訂參數；內建參數不能刪除。
BUILTIN_PARAMS = [
    {"id": "maTol", "label": "月線容許誤差", "value": 0.5, "unit": "%", "min": 0, "max": 10,
     "note": "低於月線但在此範圍內不算跌破"},
    {"id": "streakDays", "label": "法人連續買超天數 ≥", "value": 2, "unit": "日", "min": 1, "max": 20, "note": ""},
    {"id": "amplify", "label": "單日買超放大倍數 ≥", "value": 2, "unit": "倍", "min": 1, "max": 20,
     "note": "當日買超 ÷ 前 4 日平均絕對值"},
    {"id": "volRatio", "label": "量能門檻（5 日均量倍數）>", "value": 1, "unit": "倍", "min": 0, "max": 10, "note": ""},
    {"id": "marginFlat", "label": "融資持平容許增量 ≤", "value": 0, "unit": "張", "min": None, "max": None, "note": ""},
    {"id": "divergeMargin", "label": "背離：融資增加超過 >", "value": 0, "unit": "張", "min": None, "max": None, "note": ""},
    {"id": "blackPct", "label": "長黑跌幅 ≥", "value": 3, "unit": "%", "min": 0, "max": 10, "note": ""},
    {"id": "blowoff", "label": "爆量倍數 ≥", "value": 1.5, "unit": "倍", "min": 1, "max": 20, "note": ""},
]

from .rules import ENGINE_PARAMS, default_playbooks, validate_rules  # noqa: E402

BUILTIN_PARAMS += ENGINE_PARAMS

COLOR_KEYS = ("up", "down", "flat", "add", "watch", "noAdd", "keep", "reduce", "exit", "na")
COLOR_PRESETS = {
    # 台股慣例：多紅空綠
    "tw": {"up": "#d32f2f", "down": "#2e7d32", "flat": "#757575", "add": "#d32f2f", "watch": "#f9a825",
           "noAdd": "#9e9e9e", "keep": "#d32f2f", "reduce": "#66bb6a", "exit": "#1b5e20", "na": "#bdbdbd"},
    # 國際慣例：綠漲紅跌
    "intl": {"up": "#2e7d32", "down": "#d32f2f", "flat": "#757575", "add": "#2e7d32", "watch": "#f9a825",
             "noAdd": "#9e9e9e", "keep": "#2e7d32", "reduce": "#ef6c00", "exit": "#b71c1c", "na": "#bdbdbd"},
}

CODE_RE = re.compile(r"[0-9A-Z]{4,6}")
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
HHMM_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}")


def default_settings() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "display": {"preset": "tw", "colors": dict(COLOR_PRESETS["tw"])},
        "sources": {"primary": ["twse", "tpex"], "fallback": ["finmind"], "options": {}},
        "schedule": ["17:40", "21:50", "23:40", "06:40"],
        "watchlist": [stock(code=c, name=n, market=m, group=g, keyLevel=k, note=note)
                      for c, n, m, g, k, note in SEED_WATCHLIST],
        "params": copy.deepcopy(BUILTIN_PARAMS),
        "conditions": [],
        "playbooks": default_playbooks(),
        "confirmDays": 1,
    }


def stock(**kw) -> dict:
    """觀察清單一筆（補齊欄位）。"""
    base = {"code": "", "name": "", "market": "TWSE", "group": "", "note": "", "keyLevel": None,
            "holding": False, "cost": None, "shares": None, "whaleLots": None}
    base.update({k: v for k, v in kw.items() if k in base})
    return base


# ---------------- 驗證 ----------------

class SettingsError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("；".join(f"{e['path']}：{e['message']}" for e in errors[:5]))
        self.errors = errors


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def validate(s: dict, max_watchlist: int = 50, providers: dict | None = None) -> list[dict]:
    """回傳錯誤清單；空清單代表合法。providers：{id: scope}，預設取已註冊的資料源。"""
    errs: list[dict] = []

    def e(path, msg):
        errs.append({"path": path, "message": msg})

    if not isinstance(s, dict):
        return [{"path": "", "message": "設定必須是 JSON 物件"}]
    if s.get("schemaVersion") != SCHEMA_VERSION:
        e("schemaVersion", f"必須是 {SCHEMA_VERSION}")

    # display
    d = s.get("display")
    if not isinstance(d, dict):
        e("display", "必須是物件")
    else:
        if d.get("preset") not in (*COLOR_PRESETS, "custom"):
            e("display.preset", f"必須是 {', '.join([*COLOR_PRESETS, 'custom'])} 之一")
        colors = d.get("colors")
        if not isinstance(colors, dict):
            e("display.colors", "必須是物件")
        else:
            for k in COLOR_KEYS:
                if not isinstance(colors.get(k), str) or not HEX_RE.fullmatch(colors[k]):
                    e(f"display.colors.{k}", "必須是 #RRGGBB 色碼")
            for k in colors:
                if k not in COLOR_KEYS:
                    e(f"display.colors.{k}", "未知的顏色項目")

    # sources
    if providers is None:
        from .providers import REGISTRY
        providers = {k: v.scope for k, v in REGISTRY.items()}
    src = s.get("sources")
    if not isinstance(src, dict):
        e("sources", "必須是物件")
    else:
        for key, scope in (("primary", "market"), ("fallback", "stock")):
            ids = src.get(key)
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                e(f"sources.{key}", "必須是字串陣列")
                continue
            if key == "primary" and not ids:
                e("sources.primary", "至少要一個資料源")
            if len(set(ids)) != len(ids):
                e(f"sources.{key}", "資料源重複")
            for i, x in enumerate(ids):
                if x not in providers:
                    e(f"sources.{key}[{i}]", f"未知的資料源 {x}（可用：{', '.join(sorted(providers))}）")
                elif providers[x] != scope:
                    e(f"sources.{key}[{i}]", f"{x} 不能放在 {key}（{key} 只接受 scope={scope} 的資料源）")
        opts = src.get("options", {})
        if not isinstance(opts, dict) or not all(isinstance(v, dict) for v in opts.values()):
            e("sources.options", "必須是 {資料源 id: {選項}} 物件")

    # schedule
    sch = s.get("schedule")
    if not isinstance(sch, list) or not 1 <= len(sch) <= 12:
        e("schedule", "必須是 1～12 個 HH:MM 時間")
    else:
        for i, t in enumerate(sch):
            if not isinstance(t, str) or not HHMM_RE.fullmatch(t):
                e(f"schedule[{i}]", "時間格式必須是 HH:MM（24 小時制）")
        if len(set(sch)) != len(sch):
            e("schedule", "時間重複")

    # watchlist
    wl = s.get("watchlist")
    if not isinstance(wl, list):
        e("watchlist", "必須是陣列")
    else:
        if len(wl) > max_watchlist:
            e("watchlist", f"最多 {max_watchlist} 檔（目前 {len(wl)} 檔）")
        seen = set()
        for i, w in enumerate(wl):
            p = f"watchlist[{i}]"
            if not isinstance(w, dict):
                e(p, "必須是物件")
                continue
            code = w.get("code")
            if not isinstance(code, str) or not CODE_RE.fullmatch(code):
                e(f"{p}.code", "股票代號必須是 4～6 碼英數字（大寫）")
            elif code in seen:
                e(f"{p}.code", f"{code} 重複")
            seen.add(code)
            if not isinstance(w.get("name"), str) or not w["name"].strip():
                e(f"{p}.name", "名稱不可空白")
            if w.get("market") not in ("TWSE", "TPEx"):
                e(f"{p}.market", "必須是 TWSE（上市）或 TPEx（上櫃）")
            for k in ("group", "note"):
                if not isinstance(w.get(k, ""), str):
                    e(f"{p}.{k}", "必須是文字")
            for k in ("keyLevel", "cost"):
                v = w.get(k)
                if v is not None and (not _num(v) or v <= 0):
                    e(f"{p}.{k}", "必須是正數或 null")
            for k in ("shares", "whaleLots"):
                v = w.get(k)
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v <= 0):
                    e(f"{p}.{k}", "必須是正整數或 null")
            if not isinstance(w.get("holding", False), bool):
                e(f"{p}.holding", "必須是 true 或 false")
            for k in w:
                if k not in stock():
                    e(f"{p}.{k}", "未知的欄位")

    # params
    ps = s.get("params")
    if not isinstance(ps, list):
        e("params", "必須是陣列")
    else:
        ids = set()
        for i, prm in enumerate(ps):
            p = f"params[{i}]"
            if not isinstance(prm, dict):
                e(p, "必須是物件")
                continue
            pid = prm.get("id")
            if not isinstance(pid, str) or not ID_RE.fullmatch(pid):
                e(f"{p}.id", "必須是英文字母開頭的識別名稱（英數字與底線，最多 40 字）")
            elif pid in ids:
                e(f"{p}.id", f"{pid} 重複")
            ids.add(pid)
            if not isinstance(prm.get("label"), str) or not prm["label"].strip():
                e(f"{p}.label", "名稱不可空白")
            v = prm.get("value")
            if not _num(v):
                e(f"{p}.value", "必須是數字")
            for k in ("min", "max"):
                if prm.get(k) is not None and not _num(prm[k]):
                    e(f"{p}.{k}", "必須是數字或 null")
            if _num(v):
                if _num(prm.get("min")) and v < prm["min"]:
                    e(f"{p}.value", f"不可小於 {prm['min']}")
                if _num(prm.get("max")) and v > prm["max"]:
                    e(f"{p}.value", f"不可大於 {prm['max']}")
            for k in ("unit", "note"):
                if not isinstance(prm.get(k, ""), str):
                    e(f"{p}.{k}", "必須是文字")
        for b in BUILTIN_PARAMS:  # normalize() 會自動補回；直接呼叫 validate 時才會遇到
            if b["id"] not in ids:
                e("params", f"內建參數 {b['id']}（{b['label']}）不能刪除")

    # conditions / playbooks
    if not isinstance(s.get("conditions"), list):
        e("conditions", "必須是陣列")
    pb = s.get("playbooks")
    if not isinstance(pb, dict) or not all(isinstance(pb.get(k), list) for k in ("entry", "hold")):
        e("playbooks", "必須包含 entry 與 hold 兩個陣列")
    validate_rules(s, e)
    cd = s.get("confirmDays")
    if not isinstance(cd, int) or isinstance(cd, bool) or not 1 <= cd <= 5:
        e("confirmDays", "必須是 1～5 的整數")

    for k in s:
        if k not in default_settings():
            e(k, "未知的設定項目")
    return errs


def normalize(s: dict) -> dict:
    """補齊缺少的項目（新版本新增的參數、欄位），不改使用者已設定的值。"""
    s = copy.deepcopy(s)
    dflt = default_settings()
    for k, v in dflt.items():
        if k not in s:
            s[k] = copy.deepcopy(v) if k != "watchlist" else []
    if isinstance(s.get("display"), dict):
        s["display"].setdefault("preset", "tw")
        colors = s["display"].setdefault("colors", {})
        if isinstance(colors, dict):
            base = COLOR_PRESETS.get(s["display"]["preset"], COLOR_PRESETS["tw"])
            for k in COLOR_KEYS:
                colors.setdefault(k, base[k])
    if isinstance(s.get("sources"), dict):
        s["sources"].setdefault("options", {})
    if isinstance(s.get("playbooks"), dict):  # v0.2 P2 以前沒有規則：補上預設規則
        for k, v in default_playbooks().items():
            if not s["playbooks"].get(k):
                s["playbooks"][k] = v
    if isinstance(s.get("params"), list):
        have = {p.get("id") for p in s["params"] if isinstance(p, dict)}
        s["params"] += [copy.deepcopy(b) for b in BUILTIN_PARAMS if b["id"] not in have]
        for p in s["params"]:
            if isinstance(p, dict):
                p.setdefault("unit", "")
                p.setdefault("note", "")
                p.setdefault("min", None)
                p.setdefault("max", None)
    if isinstance(s.get("watchlist"), list):
        s["watchlist"] = [stock(**w) if isinstance(w, dict) else w for w in s["watchlist"]]
    return s


def _is_sensitive(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(x in k for x in SENSITIVE_KEYS)


def mask(s: dict) -> dict:
    """把資料源選項中的 token／密碼遮蔽，只留後 4 碼。"""
    s = copy.deepcopy(s)
    for opts in (s.get("sources", {}).get("options", {}) or {}).values():
        for k, v in list(opts.items()):
            if _is_sensitive(k) and isinstance(v, str) and v:
                opts[k] = MASK + v[-4:]
    return s


def unmask(new: dict, old: dict) -> dict:
    """網頁送回的遮蔽值代表「不變」，換回原本的值。"""
    new = copy.deepcopy(new)
    old_opts = (old.get("sources", {}) or {}).get("options", {}) or {}
    new_opts = (new.get("sources", {}) or {}).get("options", {}) if isinstance(new.get("sources"), dict) else {}
    for pid, opts in (new_opts or {}).items():
        if not isinstance(opts, dict):
            continue
        for k, v in list(opts.items()):
            if _is_sensitive(k) and isinstance(v, str) and v.startswith(MASK):
                prev = (old_opts.get(pid) or {}).get(k)
                if isinstance(prev, str) and prev.endswith(v[len(MASK):]):
                    opts[k] = prev
    return new


# ---------------- 儲存 ----------------

class SettingsStore:
    def __init__(self, path: Path | str, max_watchlist: int = 50, migrate=None):
        """migrate：檔案不存在時呼叫，回傳初始設定（例如從 v0.1 遷移）。"""
        self.path = Path(path)
        self.hist_dir = self.path.with_name(self.path.stem + ".history")
        self.max_watchlist = max_watchlist
        self._lock = threading.RLock()
        self._data: dict = {}
        self._mtime = 0.0
        self.rev = 0
        self.last_error: list[dict] | None = None
        if self.path.exists():
            self._reload(force=True)
            if not self._data:  # 檔案壞掉且沒有合法版本：用預設值啟動，原檔備份
                bad = self.path.with_suffix(f".invalid-{datetime.now():%Y%m%d%H%M%S}.json")
                os.replace(self.path, bad)
                log.error("settings.json 不合法，已改名為 %s 並使用預設值", bad.name)
                self._write(default_settings(), keep_history=False)
        else:
            init = normalize(migrate() if migrate else default_settings())
            errs = validate(init, max_watchlist)
            if errs:  # 舊資料不合法：盡量保留觀察清單以外，退回預設
                log.error("遷移的設定不合法，改用預設值：%s", errs[:3])
                init = default_settings()
            self._write(init, keep_history=False)

    # ---- 讀取 ----
    def _reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            self.last_error = [{"path": "", "message": f"settings.json 無法解析：{ex}"}]
            log.error("%s", self.last_error[0]["message"])
            return
        data = normalize(raw) if isinstance(raw, dict) else raw
        errs = validate(data, self.max_watchlist)
        if errs:
            self.last_error = errs
            log.error("settings.json 不合法，沿用上一個合法版本：%s", errs[:3])
            return
        self.last_error = None
        self._data = data
        self.rev += 1

    def get(self) -> dict:
        with self._lock:
            self._reload()
            return copy.deepcopy(self._data)

    def watchlist(self) -> list[dict]:
        return self.get()["watchlist"]

    def params(self) -> dict:
        return {p["id"]: p["value"] for p in self.get()["params"]}

    def schedule(self) -> list[str]:
        return self.get()["schedule"]

    def source_plan(self, env: dict | None = None) -> tuple[list[str], list[str], dict]:
        src = self.get()["sources"]
        opts = {k: dict(v) for k, v in src.get("options", {}).items()}
        if env and env.get("finmind_token"):
            opts.setdefault("finmind", {})["token"] = env["finmind_token"]
        return list(src["primary"]), list(src["fallback"]), opts

    # ---- 寫入 ----
    def save(self, new: dict, *, unmask_from_current: bool = True) -> dict:
        """驗證後寫入；不合法丟 SettingsError。回傳寫入後的設定。"""
        with self._lock:
            self._reload()
            if unmask_from_current:
                new = unmask(new, self._data)
            new = normalize(new)
            errs = validate(new, self.max_watchlist)
            if errs:
                raise SettingsError(errs)
            self._write(new)
            return copy.deepcopy(self._data)

    def update(self, fn) -> dict:
        """讀取 → fn(設定) 修改 → 驗證寫入，全部在同一把鎖內。"""
        with self._lock:
            data = self.get()
            fn(data)
            return self.save(data, unmask_from_current=False)

    def _write(self, data: dict, keep_history: bool = True) -> None:
        if keep_history and self.path.exists():
            self.hist_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            os.replace(self.path, self.hist_dir / f"settings-{stamp}.json")
            for old in sorted(self.hist_dir.glob("settings-*.json"))[:-HISTORY_KEEP]:
                old.unlink(missing_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # 可能含資料源 token
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._data = copy.deepcopy(data)
        self._mtime = self.path.stat().st_mtime
        self.last_error = None
        self.rev += 1

    # ---- 版本 ----
    def history(self) -> list[dict]:
        if not self.hist_dir.exists():
            return []
        out = []
        for f in sorted(self.hist_dir.glob("settings-*.json"), reverse=True):
            m = re.fullmatch(r"settings-(\d{8})-(\d{6})-\d+\.json", f.name)
            at = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").isoformat() if m else None
            out.append({"id": f.stem.removeprefix("settings-"), "savedAt": at, "size": f.stat().st_size})
        return out

    def restore(self, hist_id: str) -> dict:
        if not re.fullmatch(r"\d{8}-\d{6}-\d+", hist_id or ""):
            raise SettingsError([{"path": "id", "message": "版本編號格式錯誤"}])
        f = self.hist_dir / f"settings-{hist_id}.json"
        if not f.exists():
            raise SettingsError([{"path": "id", "message": "找不到這個版本"}])
        return self.save(json.loads(f.read_text(encoding="utf-8")), unmask_from_current=False)

    def reset(self, section: str) -> dict:
        dflt = default_settings()
        if section not in dflt or section in ("schemaVersion", "watchlist"):
            raise SettingsError([{"path": "section", "message": "可重設：" + "、".join(
                k for k in dflt if k not in ("schemaVersion", "watchlist"))}])

        def fn(s):
            s[section] = dflt[section]
        return self.update(fn)


# ---------------- v0.1 遷移 ----------------

def migrate_v01(db, legacy: dict, params_fallback: dict | None = None) -> dict:
    """把 v0.1.x 的觀察清單（SQLite stocks）、門檻（SQLite settings.rules）、
    排程與資料源（config.json）轉成 settings.json。"""
    s = default_settings()
    rows = []
    if db is not None:
        try:
            rows = [dict(r) for r in db.q("SELECT * FROM stocks ORDER BY sort_order, code")]
        except Exception:
            rows = []
        seeded = db.get_setting("seeded")
        if rows or seeded:
            s["watchlist"] = [stock(code=r["code"], name=r["name"], market=r["market"] or "TWSE",
                                    group=r.get("grp") or "", note=r.get("note") or "", keyLevel=r.get("key_level"))
                              for r in rows]
        old_rules = db.get_setting("rules", {}) or {}
        for p in s["params"]:
            if _num(old_rules.get(p["id"])):
                p["value"] = old_rules[p["id"]]
    if legacy.get("schedule"):
        s["schedule"] = list(legacy["schedule"])
    if isinstance(legacy.get("sources"), dict):
        for k in ("primary", "fallback", "options"):
            if k in legacy["sources"]:
                s["sources"][k] = legacy["sources"][k]
    if legacy.get("use_finmind_fallback") is False:
        s["sources"]["fallback"] = []
    if legacy.get("finmind_token"):
        s["sources"]["options"].setdefault("finmind", {})["token"] = legacy["finmind_token"]
    return s


def open_settings(cfg, db=None) -> SettingsStore:
    """開啟 settings.json；不存在時從 v0.1 遷移，成功後把 config.json 的舊欄位清掉。"""
    from .config import save_config
    st = SettingsStore(cfg.settings_path, cfg.max_watchlist, migrate=lambda: migrate_v01(db, cfg.legacy))
    if cfg.legacy:
        cfg.legacy = {}
        save_config(cfg)
    return st
