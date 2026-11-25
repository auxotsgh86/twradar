"""永豐證券 Shioaji：只用來讀逐筆成交（歷史 ticks），算每天的主動買賣與大單。選用，沒安裝 shioaji 也不影響其他功能。

安全：只登入、讀行情；不啟用憑證（activate_ca）、不呼叫任何下單或帳務函式。API Key 放在 config.json（權限 600）
或環境變數 SJ_API_KEY／SJ_SEC_KEY，不寫進記錄檔。

限制（官方「使用限制」，2026-11）：
- 行情查詢（ticks、kbars、snapshots…）合計每 10 秒最多 50 次；超過會暫停 1 分鐘 → 每次間隔 ≥ 0.25 秒。
- 每日流量：近 30 日沒有 API 成交的帳戶 500 MB；超過後 ticks 回傳空值。每個交易日 08:00 重置。
- 歷史 ticks：股票從 2020-03-02 起。tick_type：1＝外盤（買方主動）、2＝內盤（賣方主動）、0＝無法判斷。
"""
from __future__ import annotations

import time

BIG_VALUE = 5_000_000        # 單筆成交金額 ≥ 500 萬元＝大單（100 萬太低：台積電 1 張就超過）
HUGE_VALUE = 20_000_000      # ≥ 2000 萬元＝特大單
CLOSE_AUCTION = 13 * 3600 + 25 * 60   # 13:25 之後是收盤集合競價
TICK_BYTES = 64              # 估算流量：每筆約 8 個欄位 × 8 bytes
MIN_INTERVAL = 0.25          # 每 10 秒最多 50 次


class SinopacError(RuntimeError):
    pass


def _tod(ts) -> int | None:
    """ticks 的 ts：奈秒，台北時間當成 UTC 編碼 → 當天第幾秒。"""
    try:
        return int(ts // 1_000_000_000) % 86400
    except (TypeError, ValueError):
        return None


def aggregate(t) -> dict:
    """一天的 ticks（物件或 dict，欄位 close、volume（張）、tick_type，可有 ts）→ 每日彙總（張）。
    開盤第一筆與 13:25 之後（收盤集合競價）是撮合、沒有主動方，算「無法判斷」，不算外盤／內盤。"""
    g = (lambda k: t.get(k)) if isinstance(t, dict) else (lambda k: getattr(t, k, None))
    close, vol, typ = list(g("close")), list(g("volume")), list(g("tick_type"))
    ts = list(g("ts") or []) or [None] * len(close)
    out = {"n": len(close), "vol": 0, "buy": 0, "sell": 0, "unk": 0, "bigBuy": 0, "bigSell": 0, "hugeBuy": 0, "hugeSell": 0,
           "amount": 0.0}
    for j, (p, v, k, x) in enumerate(zip(close, vol, typ, ts)):
        if not v or p is None:
            continue
        val = p * v * 1000
        out["vol"] += v
        out["amount"] += val
        tod = _tod(x) if x is not None else None
        auction = j == 0 or (tod is not None and tod >= CLOSE_AUCTION)
        side = "unk" if auction else "buy" if k == 1 else "sell" if k == 2 else "unk"
        out[side] += v
        if side != "unk" and val >= BIG_VALUE:
            out["big" + side.title()] += v
            if val >= HUGE_VALUE:
                out["huge" + side.title()] += v
    out["vwap"] = round(out["amount"] / (out["vol"] * 1000), 4) if out["vol"] else None
    out["amount"] = round(out["amount"])
    return out


def login(api, api_key: str, secret_key: str):
    """各版本 shioaji 的 login 參數不同：只傳這一版有的參數（新版沒有 contracts_timeout）。"""
    import inspect
    kw = {"api_key": api_key, "secret_key": secret_key}
    try:
        params = inspect.signature(api.login).parameters
    except (TypeError, ValueError):
        params = {}
    if "contracts_timeout" in params:
        kw["contracts_timeout"] = 30000
    if "fetch_contract" in params:
        kw["fetch_contract"] = True
    return api.login(**kw)


class Sinopac:
    """api：已登入的 shioaji.Shioaji（測試時可換成假的物件）。"""

    def __init__(self, api, sleep=time.sleep, clock=time.monotonic):
        self.api, self.sleep, self.clock = api, sleep, clock
        self._last = 0.0
        self.bytes = 0
        self.calls = 0

    @classmethod
    def login(cls, api_key: str, secret_key: str) -> "Sinopac":
        if not api_key or not secret_key:
            raise SinopacError("沒有設定永豐 API Key（config.json 的 sinopac_api_key／sinopac_secret_key，或環境變數 SJ_API_KEY／SJ_SEC_KEY）")
        try:
            import shioaji as sj
        except ImportError as e:
            raise SinopacError("沒有安裝 shioaji：在 config.json 填好 sinopac_api_key 後重新執行 ./deploy/install.sh，"
                               "並用 ~/twradar-app/.venv/bin/python 執行") from e
        api = sj.Shioaji(simulation=False)
        login(api, api_key, secret_key)
        return cls(api)

    def usage(self) -> dict | None:
        """官方的流量用量（新版 shioaji 有 api.usage()）：{"bytes", "limit", "remaining"}；沒有就回傳 None。"""
        f = getattr(self.api, "usage", None)
        if not f:
            return None
        try:
            u = f()
        except Exception:  # noqa: BLE001
            return None
        get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
        b, lim, rem = get("bytes"), get("limit_bytes"), get("remaining_bytes")
        if b is None and rem is None:
            return None
        return {"bytes": b, "limit": lim, "remaining": rem}

    def logout(self) -> None:
        try:
            self.api.logout()
        except Exception:  # noqa: BLE001
            pass

    def contract(self, code: str):
        c = None
        get = getattr(getattr(self.api, "contracts", None), "get", None)
        if get:
            try:
                c = get(code)
            except Exception:  # noqa: BLE001
                c = None
        if c is None:
            try:
                c = self.api.Contracts.Stocks[code]
            except Exception:  # noqa: BLE001
                c = None
        return c

    def ticks(self, code: str, date: str):
        """date：YYYYMMDD。回傳 shioaji 的 Ticks；找不到商品回傳 None。"""
        c = self.contract(code)
        if c is None:
            return None
        wait = self._last + MIN_INTERVAL - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()
        self.calls += 1
        t = self.api.ticks(contract=c, date=f"{date[:4]}-{date[4:6]}-{date[6:]}")
        n = len(t["close"] if isinstance(t, dict) else t.close)
        self.bytes += n * TICK_BYTES
        return t
