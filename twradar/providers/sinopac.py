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

BIG_VALUE = 1_000_000        # 單筆成交金額 ≥ 100 萬元＝大單
HUGE_VALUE = 5_000_000       # ≥ 500 萬元＝特大單
TICK_BYTES = 64              # 估算流量：每筆約 8 個欄位 × 8 bytes
MIN_INTERVAL = 0.25          # 每 10 秒最多 50 次


class SinopacError(RuntimeError):
    pass


def aggregate(t) -> dict:
    """一天的 ticks（物件或 dict，欄位 close、volume（張）、tick_type）→ 每日彙總（張）。"""
    g = (lambda k: t[k]) if isinstance(t, dict) else (lambda k: getattr(t, k))
    close, vol, typ = list(g("close")), list(g("volume")), list(g("tick_type"))
    out = {"n": len(close), "vol": 0, "buy": 0, "sell": 0, "unk": 0, "bigBuy": 0, "bigSell": 0, "hugeBuy": 0, "hugeSell": 0,
           "amount": 0.0}
    for p, v, k in zip(close, vol, typ):
        if not v or p is None:
            continue
        val = p * v * 1000
        out["vol"] += v
        out["amount"] += val
        side = "buy" if k == 1 else "sell" if k == 2 else "unk"
        out[side] += v
        if side != "unk" and val >= BIG_VALUE:
            out["big" + side.title()] += v
            if val >= HUGE_VALUE:
                out["huge" + side.title()] += v
    out["vwap"] = round(out["amount"] / (out["vol"] * 1000), 4) if out["vol"] else None
    out["amount"] = round(out["amount"])
    return out


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
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=30000)
        return cls(api)

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
