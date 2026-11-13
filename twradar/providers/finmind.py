"""FinMind：逐檔抓取，用於官方來源失敗時的備援與補歷史。"""
from __future__ import annotations

from ..dates import dash, norm_date
from ..net import FetchError, inum, num
from .base import Batch, Provider, register

DATASET_API = {"quotes": "TaiwanStockPrice", "inst": "TaiwanStockInstitutionalInvestorsBuySell",
               "margin": "TaiwanStockMarginPurchaseShortSale"}


@register
class FinMind(Provider):
    id = "finmind"
    label = "FinMind"
    scope = "stock"
    datasets = ("quotes", "inst", "margin")
    base = "https://api.finmindtrade.com/api/v4/data"

    def _get(self, dataset: str, code: str, start: str, end: str) -> list[dict]:
        url = f"{self.base}?dataset={DATASET_API[dataset]}&data_id={code}&start_date={dash(start)}&end_date={dash(end)}"
        if self.options.get("token"):
            url += f"&token={self.options['token']}"
        js = self.t.get_json(url, retries=0)
        if not isinstance(js, dict) or js.get("status") not in (200, "200"):
            raise FetchError("http", f"FinMind {DATASET_API[dataset]}: {js.get('msg') if isinstance(js, dict) else js}", url)
        return js.get("data") or []

    def fetch_stock(self, code: str, market: str, start: str, end: str,
                    datasets: tuple[str, ...] | None = None) -> Batch:
        want = datasets or self.datasets
        out: Batch = {}
        if "quotes" in want:
            out["quotes"] = [{"date": norm_date(r.get("date")), "code": code, "name": None, "market": market,
                              "open": num(r.get("open")), "high": num(r.get("max")), "low": num(r.get("min")),
                              "close": num(r.get("close")), "change": num(r.get("spread")),
                              "volume": inum(r.get("Trading_Volume")), "value": inum(r.get("Trading_money")), "src": "FinMind"}
                             for r in self._get("quotes", code, start, end)]
        if "inst" in want:
            agg: dict[str, dict] = {}
            for r in self._get("inst", code, start, end):
                dd = norm_date(r.get("date"))
                a = agg.setdefault(dd, {"date": dd, "code": code, "foreign_net": 0, "trust_net": 0, "dealer_net": 0,
                                        "total_net": 0, "src": "FinMind"})
                net = (inum(r.get("buy")) or 0) - (inum(r.get("sell")) or 0)
                name = str(r.get("name", ""))
                if name == "Foreign_Investor":
                    a["foreign_net"] += net
                elif name == "Investment_Trust":
                    a["trust_net"] += net
                elif name in ("Dealer_self", "Dealer_Hedging"):
                    a["dealer_net"] += net
                if name != "Foreign_Dealer_Self":
                    a["total_net"] += net
            out["inst"] = list(agg.values())
        if "margin" in want:
            out["margin"] = [{"date": norm_date(r.get("date")), "code": code,
                              "margin_prev": inum(r.get("MarginPurchaseYesterdayBalance")),
                              "margin_bal": inum(r.get("MarginPurchaseTodayBalance")),
                              "short_prev": inum(r.get("ShortSaleYesterdayBalance")),
                              "short_bal": inum(r.get("ShortSaleTodayBalance")), "src": "FinMind"}
                             for r in self._get("margin", code, start, end)]
        return out

    def doctor(self, d: str):
        return [(f"{self.label} 備援（6488）",
                 lambda: len(self.fetch_stock("6488", "TPEx", d, d, ("quotes",))["quotes"]))]
