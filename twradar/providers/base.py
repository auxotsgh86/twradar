"""資料源介面。

每個資料源（Provider）把外部資料轉成標準列（dict），依「資料表」分組回傳：

    {"quotes": [...], "inst": [...], "margin": [...], "market": [...]}

標準列格式（單位在這裡統一，Provider 負責轉換）：

    quotes  date, code, name, market, open, high, low, close, change, volume(股), value(元), src
    inst    date, code, foreign_net, trust_net, dealer_net, total_net（皆為股）, src
    margin  date, code, margin_prev, margin_bal, short_prev, short_bal（皆為張）, src
    market  date, taiex, taiex_chg, taiex_chg_pct, foreign_amt, trust_amt, dealer_amt, total_amt（元）, src

兩種範圍：
- scope="market"：一次抓某交易所某天的全市場資料（證交所、櫃買中心）。
- scope="stock"：逐檔抓一段期間（FinMind），用於備援與補歷史。
- scope="weekly"：每週資料（集保股權分散表），fetch_week() 回傳最新一週。

HTTP 由外部注入（Transport），測試時可換成假的。
"""
from __future__ import annotations

from typing import Protocol

Batch = dict[str, list[dict]]

# 資料集 → 寫入的資料表
DATASET_TABLE = {"quotes": "quotes", "inst": "inst", "margin": "margin", "flows": "market"}
DATASET_LABEL = {"quotes": "收盤行情", "inst": "三大法人", "margin": "融資融券", "flows": "三大法人金額"}


class Transport(Protocol):
    def get_json(self, url: str, retries: int = 1): ...


class Provider:
    id: str = ""
    label: str = ""
    scope: str = "market"            # "market" | "stock" | "weekly"
    markets: tuple[str, ...] = ()    # 涵蓋的市場（"TWSE"、"TPEx"）；空 = 不分市場
    datasets: tuple[str, ...] = ()   # 依抓取順序排列
    base: str = ""

    def __init__(self, transport: Transport, options: dict | None = None):
        self.t = transport
        self.options = options or {}
        self.base = self.options.get("base", self.base)

    def step_label(self, dataset: str) -> str:
        return f"{self.label}{DATASET_LABEL.get(dataset, dataset)}"

    # ---- scope="market" ----
    def fetch(self, dataset: str, d: str) -> Batch:
        raise NotImplementedError

    def confirm_no_trading(self, d: str) -> bool:
        """第一個資料集查無資料時，用來確認是否休市。預設不判斷。"""
        return False

    # ---- scope="stock" ----
    def fetch_stock(self, code: str, market: str, start: str, end: str,
                    datasets: tuple[str, ...] | None = None) -> Batch:
        raise NotImplementedError

    # ---- scope="weekly" ----
    def fetch_week(self, codes: set[str] | None = None) -> Batch:
        raise NotImplementedError

    # ---- 診斷（twradar doctor） ----
    def doctor(self, d: str) -> list[tuple[str, object]]:
        """回傳 [(項目名稱, 呼叫後回傳摘要的函式)]。"""
        if self.scope == "market":
            return [(self.step_label(ds), lambda ds=ds: sum(len(v) for v in self.fetch(ds, d).values()))
                    for ds in self.datasets]
        return []


REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    if not cls.id or cls.id in REGISTRY:
        raise ValueError(f"資料源 id 無效或重複：{cls.id!r}")
    REGISTRY[cls.id] = cls
    return cls


def build(ids: list[str], transport: Transport, options: dict | None = None) -> list[Provider]:
    """依 id 建立資料源；options 以資料源 id 為鍵，例如 {"finmind": {"token": "..."}}。"""
    options = options or {}
    out = []
    for i in ids:
        if i not in REGISTRY:
            raise ValueError(f"未知的資料源：{i}（可用：{', '.join(sorted(REGISTRY))}）")
        out.append(REGISTRY[i](transport, options.get(i)))
    return out
