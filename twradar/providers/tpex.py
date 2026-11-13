"""證券櫃檯買賣中心（上櫃）。新舊網址都會嘗試，以欄位名稱比對為主、位置為輔。"""
from __future__ import annotations

from ..dates import roc, slash
from ..net import FetchError, inum, num
from .base import Batch, Provider, register
from .common import CODE_RE, cell, check_date, col, tables

QUOTE_URLS = [
    # 2026/11 實機確認：前兩個會依日期回傳；最後一個只回傳最新一天（僅供當日備用）
    "{b}/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json",
    "{b}/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=EW&o=json",
    "{b}/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc}&o=json",
]
INST_URLS = [
    "{b}/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash}&response=json",
    "{b}/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={roc}&o=json",
]
MARGIN_URLS = [
    "{b}/www/zh-tw/margin/balance?date={slash}&response=json",
    "{b}/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&d={roc}&o=json",
]


@register
class TPEx(Provider):
    id = "tpex"
    label = "上櫃"
    scope = "market"
    markets = ("TPEx",)
    datasets = ("quotes", "inst", "margin")
    base = "https://www.tpex.org.tw"

    def urls(self, templates: list[str], d: str) -> list[str]:
        return [u.format(b=self.base, slash=slash(d), roc=roc(d)) for u in templates]

    def quote_urls(self, d: str) -> list[str]:
        return self.urls(QUOTE_URLS, d)

    def fetch(self, dataset: str, d: str) -> Batch:
        return getattr(self, "_" + dataset)(d)

    def _try(self, urls: list[str], d: str):
        """依序嘗試多個網址。優先回報「尚未公布」，其次「格式／參數錯誤」。"""
        errs: list[FetchError] = []
        for u in urls:
            try:
                js = self.t.get_json(u, retries=0)
                check_date(js, d, u)
            except FetchError as e:
                errs.append(e)
                continue
            if any(rows for _, _, rows in tables(js)):
                return js, u
            errs.append(FetchError("nodata", f"櫃買中心 {d} 沒有資料", u))
        for kind in ("nodata", "parse", "blocked", "http", "network"):
            for e in errs:
                if e.kind == kind:
                    raise e
        raise FetchError("nodata", "櫃買中心沒有資料")

    def _quotes(self, d: str) -> Batch:
        js, url = self._try(self.quote_urls(d), d)
        src = "TPEx " + url.split("?")[0].rstrip("/").split("/")[-1]
        for title, fields, data in tables(js):
            if not data or len(data[0]) < 9:
                continue
            ic, iname = col(fields, "代號"), col(fields, "名稱")
            iclose, ichg = col(fields, "收盤"), col(fields, "漲跌")
            iopen, ihigh, ilow = col(fields, "開盤"), col(fields, "最高"), col(fields, "最低")
            ivol, ival = col(fields, "成交股數"), col(fields, "成交金額")
            if iclose is None:  # 舊版沒有欄位名稱：代號 名稱 收盤 漲跌 開盤 最高 最低 均價 成交股數 成交金額 成交筆數
                ic, iname, iclose, ichg, iopen, ihigh, ilow, ivol, ival = 0, 1, 2, 3, 4, 5, 6, 8, 9
            if ic is None:
                ic, iname = 0, 1
            out = []
            for r in data:
                code = str(cell(r, ic)).strip()
                if not CODE_RE.fullmatch(code):
                    continue
                out.append({"date": d, "code": code, "name": str(cell(r, iname) or "").strip(), "market": "TPEx",
                            "open": num(cell(r, iopen)), "high": num(cell(r, ihigh)), "low": num(cell(r, ilow)),
                            "close": num(cell(r, iclose)), "change": num(cell(r, ichg)),
                            "volume": inum(cell(r, ivol)), "value": inum(cell(r, ival)), "src": src})
            if out:
                return {"quotes": out}
        raise FetchError("parse", "櫃買中心收盤行情格式無法辨識")

    def _inst(self, d: str) -> Batch:
        js, url = self._try(self.urls(INST_URLS, d), d)
        for title, fields, data in tables(js):
            if not data or len(data[0]) < 14:
                continue
            i_f = col(fields, "外資及陸資", "不含", "買賣超")
            i_t = col(fields, "投信", "買賣超")
            i_d = col(fields, "自營商", "買賣超", exclude=("自行", "避險"))
            i_all = col(fields, "三大法人", "買賣超")
            if i_f is None or i_t is None:
                # 標準 24 欄：外資(不含)買/賣/超=2-4、外資自營=5-7、外資合計=8-10、投信=11-13、
                # 自營自行=14-16、自營避險=17-19、自營合計=20-22、三大法人合計=23
                if len(data[0]) >= 24:
                    i_f, i_t, i_d, i_all = 4, 13, 22, 23
                else:
                    continue
            out = []
            for r in data:
                code = str(r[0]).strip()
                if not CODE_RE.fullmatch(code):
                    continue
                out.append({"date": d, "code": code, "foreign_net": inum(cell(r, i_f)), "trust_net": inum(cell(r, i_t)),
                            "dealer_net": inum(cell(r, i_d)), "total_net": inum(cell(r, i_all)), "src": "TPEx 3insti"})
            if out:
                return {"inst": out}
        raise FetchError("parse", "櫃買中心三大法人格式無法辨識")

    def _margin(self, d: str) -> Batch:
        js, url = self._try(self.urls(MARGIN_URLS, d), d)
        for title, fields, data in tables(js):
            if not data or len(data[0]) < 15:
                continue
            i_mp = col(fields, "前資餘額")
            i_mb = col(fields, "資餘額", exclude=("前", "券"))
            i_sp = col(fields, "前券餘額")
            i_sb = col(fields, "券餘額", exclude=("前", "資"))
            if None in (i_mp, i_mb, i_sp, i_sb):
                # 舊版位置：代號 名稱 前資餘額 資買 資賣 現償 資餘額 資屬證金 資使用率 資限額 前券餘額 券賣 券買 券償 券餘額
                i_mp, i_mb, i_sp, i_sb = 2, 6, 10, 14
            out = []
            for r in data:
                code = str(r[0]).strip()
                if not CODE_RE.fullmatch(code):
                    continue
                out.append({"date": d, "code": code, "margin_prev": inum(cell(r, i_mp)), "margin_bal": inum(cell(r, i_mb)),
                            "short_prev": inum(cell(r, i_sp)), "short_bal": inum(cell(r, i_sb)), "src": "TPEx margin"})
            if out:
                return {"margin": out}
        raise FetchError("parse", "櫃買中心融資融券格式無法辨識")
