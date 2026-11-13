"""臺灣證券交易所（上市）。欄位格式已依 2026/11 實際回應確認。"""
from __future__ import annotations

from ..net import FetchError, inum, num
from .base import Batch, Provider, register
from .common import check_date, ok_stat, sign, tables


@register
class TWSE(Provider):
    id = "twse"
    label = "上市"
    scope = "market"
    markets = ("TWSE",)
    datasets = ("quotes", "inst", "margin", "flows")
    base = "https://www.twse.com.tw/rwd/zh"

    def step_label(self, dataset: str) -> str:
        return "三大法人金額" if dataset == "flows" else super().step_label(dataset)

    def fetch(self, dataset: str, d: str) -> Batch:
        return getattr(self, "_" + dataset)(d)

    def confirm_no_trading(self, d: str) -> bool:
        try:
            self._inst(d)
        except FetchError as e:
            return e.kind == "nodata"
        return False

    def _quotes(self, d: str) -> Batch:
        """每日收盤行情（全部，不含權證）＋加權指數。"""
        js = self.t.get_json(f"{self.base}/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json")
        if not ok_stat(js) or not tables(js):
            raise FetchError("nodata", f"證交所 {d} 無收盤行情")
        check_date(js, d)
        rows, market = [], []
        for title, fields, data in tables(js):
            if fields and "證券代號" in fields and "收盤價" in fields:
                ix = {name: fields.index(name) for name in fields}
                for r in data:
                    diff = num(r[ix["漲跌價差"]]) if "漲跌價差" in ix else None
                    sg = sign(r[ix["漲跌(+/-)"]]) if "漲跌(+/-)" in ix else 0
                    rows.append({"date": d, "code": str(r[ix["證券代號"]]).strip(), "name": str(r[ix["證券名稱"]]).strip(),
                                 "market": "TWSE", "open": num(r[ix["開盤價"]]), "high": num(r[ix["最高價"]]),
                                 "low": num(r[ix["最低價"]]), "close": num(r[ix["收盤價"]]),
                                 "change": (sg * diff) if diff is not None else None,
                                 "volume": inum(r[ix["成交股數"]]), "value": inum(r[ix["成交金額"]]), "src": "TWSE MI_INDEX"})
            elif fields and "價格指數(臺灣證券交易所)" in str(title) and fields[0] == "指數":
                for r in data:
                    if str(r[0]).strip() == "發行量加權股價指數":
                        close, pts = num(r[1]), num(r[3])
                        if close is not None and pts is not None:
                            chg = sign(r[2]) * pts
                            prev = close - chg
                            market = [{"date": d, "taiex": close, "taiex_chg": chg,
                                       "taiex_chg_pct": round(chg / prev * 100, 2) if prev else None, "src": "TWSE MI_INDEX"}]
        if not rows:
            raise FetchError("parse", "找不到收盤行情表格")
        return {"quotes": rows, "market": market}

    def _inst(self, d: str) -> Batch:
        js = self.t.get_json(f"{self.base}/fund/T86?date={d}&selectType=ALLBUT0999&response=json")
        if not ok_stat(js) or not js.get("data"):
            raise FetchError("nodata", f"證交所 {d} 無三大法人資料")
        check_date(js, d)
        f = js["fields"]
        i_code, i_f = f.index("證券代號"), f.index("外陸資買賣超股數(不含外資自營商)")
        i_t, i_d, i_all = f.index("投信買賣超股數"), f.index("自營商買賣超股數"), f.index("三大法人買賣超股數")
        return {"inst": [{"date": d, "code": str(r[i_code]).strip(), "foreign_net": inum(r[i_f]), "trust_net": inum(r[i_t]),
                          "dealer_net": inum(r[i_d]), "total_net": inum(r[i_all]), "src": "TWSE T86"} for r in js["data"]]}

    def _margin(self, d: str) -> Batch:
        js = self.t.get_json(f"{self.base}/marginTrading/MI_MARGN?date={d}&selectType=ALL&response=json")
        if not ok_stat(js) or not tables(js):
            raise FetchError("nodata", f"證交所 {d} 無融資融券資料")
        check_date(js, d)
        for title, fields, data in tables(js):
            # 欄位：代號 名稱 | 融資：買進 賣出 現金償還 前日餘額 今日餘額 限額 | 融券：買進 賣出 現券償還 前日餘額 今日餘額 限額 | ...
            if fields and fields[0] == "代號" and len(fields) >= 13 and data:
                return {"margin": [{"date": d, "code": str(r[0]).strip(), "margin_prev": inum(r[5]), "margin_bal": inum(r[6]),
                                    "short_prev": inum(r[11]), "short_bal": inum(r[12]), "src": "TWSE MI_MARGN"} for r in data]}
        raise FetchError("nodata", f"證交所 {d} 融資融券表格是空的")

    def _flows(self, d: str) -> Batch:
        js = self.t.get_json(f"{self.base}/fund/BFI82U?dayDate={d}&type=day&response=json")
        if not ok_stat(js) or not js.get("data"):
            raise FetchError("nodata", f"證交所 {d} 無三大法人金額統計")
        check_date(js, d)
        m = {str(r[0]).strip(): inum(r[3]) for r in js["data"]}
        dealer = None
        if "自營商(自行買賣)" in m or "自營商(避險)" in m:
            dealer = (m.get("自營商(自行買賣)") or 0) + (m.get("自營商(避險)") or 0)
        return {"market": [{"date": d, "foreign_amt": m.get("外資及陸資(不含外資自營商)"), "trust_amt": m.get("投信"),
                            "dealer_amt": dealer, "total_amt": m.get("合計"), "src": "TWSE BFI82U"}]}
