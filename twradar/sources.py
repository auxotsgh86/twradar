"""資料來源解析：證交所（TWSE）、櫃買中心（TPEx）、FinMind（備援）。

日期一律用 'YYYYMMDD' 字串。法人單位存「股」，融資券存「張」，成交量存「股」。
證交所欄位格式已依 2026/11 實際回應確認；櫃買中心新舊兩種網址都會嘗試，
並以欄位名稱比對為主、位置為輔（請在部署後執行 `twradar doctor` 確認）。
"""
from __future__ import annotations

import re
from datetime import date as Date, datetime, timedelta

from .net import FetchError, Fetcher, inum, num

TWSE = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw"
FINMIND = "https://api.finmindtrade.com/api/v4/data"


def roc(d: str) -> str:
    return f"{int(d[:4]) - 1911}/{d[4:6]}/{d[6:8]}"


def slash(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:8]}"


def dash(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def norm_date(s) -> str | None:
    """把 '20261106'、'2026/11/06'、'115/11/06'、'2026-11-06' 轉成 'YYYYMMDD'。"""
    if s is None:
        return None
    parts = re.findall(r"\d+", str(s))
    if len(parts) == 1 and len(parts[0]) == 8:
        return parts[0]
    if len(parts) >= 3:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if y < 1911:
            y += 1911
        return f"{y:04d}{m:02d}{d:02d}"
    return None


def _sign(s) -> int:
    t = re.sub(r"<[^>]+>", "", str(s or "")).strip()
    return -1 if "-" in t else (1 if "+" in t else 0)


def _ok_stat(js) -> bool:
    stat = str(js.get("stat", "OK")) if isinstance(js, dict) else "OK"
    return stat.upper() == "OK"


def _tables(js):
    """取出所有 (title, fields, rows)。支援 tables[]、fields/data、aaData。"""
    out = []
    if isinstance(js, dict):
        if isinstance(js.get("tables"), list):
            for t in js["tables"]:
                if isinstance(t, dict):
                    out.append((t.get("title", ""), t.get("fields"), t.get("data") or []))
        if "data" in js and isinstance(js.get("data"), list) and js.get("fields"):
            out.append((js.get("title", ""), js.get("fields"), js["data"]))
        if isinstance(js.get("aaData"), list):
            out.append((js.get("reportTitle", ""), js.get("fields"), js["aaData"]))
    return out


def _dates_in(js) -> list[str]:
    found = []
    if isinstance(js, dict):
        for k in ("date", "reportDate"):
            if js.get(k):
                found.append(norm_date(js[k]))
        for t in js.get("tables", []) if isinstance(js.get("tables"), list) else []:
            if isinstance(t, dict) and t.get("date"):
                found.append(norm_date(t["date"]))
    return [f for f in found if f and re.fullmatch(r"\d{8}", f)]


def _check_date(js, d: str, url: str = "") -> None:
    """回應裡有日期但不是 d 時丟出錯誤。
    回傳的日期比 d 新 → 網站沒照日期參數回應（網址或參數格式不對）→ parse 錯誤，會立刻啟用備援；
    比 d 舊 → 當日資料尚未公布 → nodata，稍後重抓。"""
    found = _dates_in(js)
    if found and d not in found:
        newest = max(found)
        if newest > d:
            raise FetchError("parse", f"網站沒有依日期回傳（要求 {d}，回傳 {newest}），可能是網址參數不對", url)
        raise FetchError("nodata", f"{d} 尚未公布（網站目前最新為 {newest}）", url)


def _col(fields, *must, exclude=()):
    """找出欄位名稱同時包含 must 所有字、且不含 exclude 任一字的欄位位置。"""
    if not fields:
        return None
    for i, f in enumerate(fields):
        s = str(f)
        if all(m in s for m in must) and not any(x in s for x in exclude):
            return i
    return None


def _cell(row, i):
    return row[i] if i is not None and i < len(row) else None


# ======================= 證交所 TWSE =======================

def twse_quotes(f: Fetcher, d: str) -> tuple[list[dict], dict]:
    """每日收盤行情（全部，不含權證）＋加權指數。"""
    js = f.get_json(f"{TWSE}/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json")
    if not _ok_stat(js) or not _tables(js):
        raise FetchError("nodata", f"證交所 {d} 無收盤行情")
    _check_date(js, d)
    rows, market = [], {}
    for title, fields, data in _tables(js):
        if fields and "證券代號" in fields and "收盤價" in fields:
            ix = {name: fields.index(name) for name in fields}
            for r in data:
                code = str(r[ix["證券代號"]]).strip()
                close = num(r[ix["收盤價"]])
                diff = num(r[ix["漲跌價差"]]) if "漲跌價差" in ix else None
                sg = _sign(r[ix["漲跌(+/-)"]]) if "漲跌(+/-)" in ix else 0
                rows.append({"date": d, "code": code, "name": str(r[ix["證券名稱"]]).strip(), "market": "TWSE",
                             "open": num(r[ix["開盤價"]]), "high": num(r[ix["最高價"]]), "low": num(r[ix["最低價"]]),
                             "close": close, "change": (sg * diff) if diff is not None else None,
                             "volume": inum(r[ix["成交股數"]]), "value": inum(r[ix["成交金額"]]), "src": "TWSE MI_INDEX"})
        elif fields and "價格指數(臺灣證券交易所)" in str(title) and fields[0] == "指數":
            for r in data:
                if str(r[0]).strip() == "發行量加權股價指數":
                    close, pts = num(r[1]), num(r[3])
                    sg = _sign(r[2])
                    if close is not None and pts is not None:
                        chg = sg * pts
                        prev = close - chg
                        market = {"date": d, "taiex": close, "taiex_chg": chg,
                                  "taiex_chg_pct": round(chg / prev * 100, 2) if prev else None, "src": "TWSE MI_INDEX"}
    if not rows:
        raise FetchError("parse", "找不到收盤行情表格")
    return rows, market


def twse_inst(f: Fetcher, d: str) -> list[dict]:
    js = f.get_json(f"{TWSE}/fund/T86?date={d}&selectType=ALLBUT0999&response=json")
    if not _ok_stat(js) or not js.get("data"):
        raise FetchError("nodata", f"證交所 {d} 無三大法人資料")
    _check_date(js, d)
    fields = js["fields"]
    i_code = fields.index("證券代號")
    i_f = fields.index("外陸資買賣超股數(不含外資自營商)")
    i_t = fields.index("投信買賣超股數")
    i_d = fields.index("自營商買賣超股數")
    i_all = fields.index("三大法人買賣超股數")
    return [{"date": d, "code": str(r[i_code]).strip(), "foreign_net": inum(r[i_f]), "trust_net": inum(r[i_t]),
             "dealer_net": inum(r[i_d]), "total_net": inum(r[i_all]), "src": "TWSE T86"} for r in js["data"]]


def twse_margin(f: Fetcher, d: str) -> list[dict]:
    js = f.get_json(f"{TWSE}/marginTrading/MI_MARGN?date={d}&selectType=ALL&response=json")
    if not _ok_stat(js) or not _tables(js):
        raise FetchError("nodata", f"證交所 {d} 無融資融券資料")
    _check_date(js, d)
    for title, fields, data in _tables(js):
        # 欄位：代號 名稱 | 融資：買進 賣出 現金償還 前日餘額 今日餘額 限額 | 融券：買進 賣出 現券償還 前日餘額 今日餘額 限額 | ...
        if fields and fields[0] == "代號" and len(fields) >= 13 and data:
            return [{"date": d, "code": str(r[0]).strip(), "margin_prev": inum(r[5]), "margin_bal": inum(r[6]),
                     "short_prev": inum(r[11]), "short_bal": inum(r[12]), "src": "TWSE MI_MARGN"} for r in data]
    raise FetchError("nodata", f"證交所 {d} 融資融券表格是空的")


def twse_flows(f: Fetcher, d: str) -> dict:
    js = f.get_json(f"{TWSE}/fund/BFI82U?dayDate={d}&type=day&response=json")
    if not _ok_stat(js) or not js.get("data"):
        raise FetchError("nodata", f"證交所 {d} 無三大法人金額統計")
    _check_date(js, d)
    m = {str(r[0]).strip(): inum(r[3]) for r in js["data"]}
    dealer = None
    if "自營商(自行買賣)" in m or "自營商(避險)" in m:
        dealer = (m.get("自營商(自行買賣)") or 0) + (m.get("自營商(避險)") or 0)
    return {"date": d, "foreign_amt": m.get("外資及陸資(不含外資自營商)"), "trust_amt": m.get("投信"),
            "dealer_amt": dealer, "total_amt": m.get("合計"), "src": "TWSE BFI82U"}


# ======================= 櫃買中心 TPEx =======================

def _tpex_try(f: Fetcher, urls: list[str], d: str):
    """依序嘗試多個網址。優先回報「尚未公布」，其次「格式／參數錯誤」。"""
    errs: list[FetchError] = []
    for u in urls:
        try:
            js = f.get_json(u, retries=0)
            _check_date(js, d, u)
        except FetchError as e:
            errs.append(e)
            continue
        if any(rows for _, _, rows in _tables(js)):
            return js, u
        errs.append(FetchError("nodata", f"櫃買中心 {d} 沒有資料", u))
    for kind in ("nodata", "parse", "blocked", "http", "network"):
        for e in errs:
            if e.kind == kind:
                raise e
    raise FetchError("nodata", "櫃買中心沒有資料")


TPEX_QUOTE_URLS = [
    # 2026/11 實機確認：前兩個會依日期回傳；最後一個只回傳最新一天（僅供當日備用）
    "{b}/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json",
    "{b}/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=EW&o=json",
    "{b}/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc}&o=json",
]


def tpex_quote_urls(d: str) -> list[str]:
    return [u.format(b=TPEX, slash=slash(d), roc=roc(d)) for u in TPEX_QUOTE_URLS]


def tpex_quotes(f: Fetcher, d: str) -> list[dict]:
    js, url = _tpex_try(f, tpex_quote_urls(d), d)
    for title, fields, data in _tables(js):
        if not data or len(data[0]) < 9:
            continue
        ic, iname = _col(fields, "代號"), _col(fields, "名稱")
        iclose, ichg = _col(fields, "收盤"), _col(fields, "漲跌")
        iopen, ihigh, ilow = _col(fields, "開盤"), _col(fields, "最高"), _col(fields, "最低")
        ivol, ival = _col(fields, "成交股數"), _col(fields, "成交金額")
        if iclose is None:  # 舊版沒有欄位名稱：代號 名稱 收盤 漲跌 開盤 最高 最低 均價 成交股數 成交金額 成交筆數
            ic, iname, iclose, ichg, iopen, ihigh, ilow, ivol, ival = 0, 1, 2, 3, 4, 5, 6, 8, 9
        if ic is None:
            ic, iname = 0, 1
        out = []
        for r in data:
            code = str(_cell(r, ic)).strip()
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            out.append({"date": d, "code": code, "name": str(_cell(r, iname) or "").strip(), "market": "TPEx",
                        "open": num(_cell(r, iopen)), "high": num(_cell(r, ihigh)), "low": num(_cell(r, ilow)),
                        "close": num(_cell(r, iclose)), "change": num(_cell(r, ichg)),
                        "volume": inum(_cell(r, ivol)), "value": inum(_cell(r, ival)), "src": "TPEx " + url.split("?")[0].rstrip("/").split("/")[-1]})
        if out:
            return out
    raise FetchError("parse", "櫃買中心收盤行情格式無法辨識")


def tpex_inst(f: Fetcher, d: str) -> list[dict]:
    js, url = _tpex_try(f, [
        f"{TPEX}/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash(d)}&response=json",
        f"{TPEX}/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={roc(d)}&o=json",
    ], d)
    for title, fields, data in _tables(js):
        if not data or len(data[0]) < 14:
            continue
        i_f = _col(fields, "外資及陸資", "不含", "買賣超")
        i_t = _col(fields, "投信", "買賣超")
        i_d = _col(fields, "自營商", "買賣超", exclude=("自行", "避險"))
        i_all = _col(fields, "三大法人", "買賣超")
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
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            out.append({"date": d, "code": code, "foreign_net": inum(_cell(r, i_f)), "trust_net": inum(_cell(r, i_t)),
                        "dealer_net": inum(_cell(r, i_d)), "total_net": inum(_cell(r, i_all)), "src": "TPEx 3insti"})
        if out:
            return out
    raise FetchError("parse", "櫃買中心三大法人格式無法辨識")


def tpex_margin(f: Fetcher, d: str) -> list[dict]:
    js, url = _tpex_try(f, [
        f"{TPEX}/www/zh-tw/margin/balance?date={slash(d)}&response=json",
        f"{TPEX}/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&d={roc(d)}&o=json",
    ], d)
    for title, fields, data in _tables(js):
        if not data or len(data[0]) < 15:
            continue
        i_mp = _col(fields, "前資餘額")
        i_mb = _col(fields, "資餘額", exclude=("前", "券"))
        i_sp = _col(fields, "前券餘額")
        i_sb = _col(fields, "券餘額", exclude=("前", "資"))
        if None in (i_mp, i_mb, i_sp, i_sb):
            # 舊版位置：代號 名稱 前資餘額 資買 資賣 現償 資餘額 資屬證金 資使用率 資限額 前券餘額 券賣 券買 券償 券餘額
            i_mp, i_mb, i_sp, i_sb = 2, 6, 10, 14
        out = []
        for r in data:
            code = str(r[0]).strip()
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            out.append({"date": d, "code": code, "margin_prev": inum(_cell(r, i_mp)), "margin_bal": inum(_cell(r, i_mb)),
                        "short_prev": inum(_cell(r, i_sp)), "short_bal": inum(_cell(r, i_sb)), "src": "TPEx margin"})
        if out:
            return out
    raise FetchError("parse", "櫃買中心融資融券格式無法辨識")


# ======================= FinMind（逐檔備援、補歷史） =======================

def _finmind(f: Fetcher, dataset: str, code: str, start: str, end: str, token: str) -> list[dict]:
    url = f"{FINMIND}?dataset={dataset}&data_id={code}&start_date={dash(start)}&end_date={dash(end)}"
    if token:
        url += f"&token={token}"
    js = f.get_json(url, retries=0)
    if not isinstance(js, dict) or js.get("status") not in (200, "200"):
        raise FetchError("http", f"FinMind {dataset}: {js.get('msg') if isinstance(js, dict) else js}", url)
    return js.get("data") or []


def finmind_stock(f: Fetcher, code: str, market: str, start: str, end: str, token: str = "") -> dict:
    """回傳 {'quotes': [...], 'inst': [...], 'margin': [...]}，每列已是資料庫格式。"""
    q = []
    for r in _finmind(f, "TaiwanStockPrice", code, start, end, token):
        dd = norm_date(r.get("date"))
        q.append({"date": dd, "code": code, "name": None, "market": market, "open": num(r.get("open")),
                  "high": num(r.get("max")), "low": num(r.get("min")), "close": num(r.get("close")),
                  "change": num(r.get("spread")), "volume": inum(r.get("Trading_Volume")),
                  "value": inum(r.get("Trading_money")), "src": "FinMind"})
    agg: dict[str, dict] = {}
    for r in _finmind(f, "TaiwanStockInstitutionalInvestorsBuySell", code, start, end, token):
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
    m = []
    for r in _finmind(f, "TaiwanStockMarginPurchaseShortSale", code, start, end, token):
        dd = norm_date(r.get("date"))
        m.append({"date": dd, "code": code, "margin_prev": inum(r.get("MarginPurchaseYesterdayBalance")),
                  "margin_bal": inum(r.get("MarginPurchaseTodayBalance")),
                  "short_prev": inum(r.get("ShortSaleYesterdayBalance")),
                  "short_bal": inum(r.get("ShortSaleTodayBalance")), "src": "FinMind"})
    return {"quotes": q, "inst": list(agg.values()), "margin": m}


def weekdays_back(end: str, n: int) -> list[str]:
    """從 end（含）往回 n 個平日（不判斷國定假日）。"""
    d = datetime.strptime(end, "%Y%m%d").date()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def is_weekend(d: str) -> bool:
    return datetime.strptime(d, "%Y%m%d").date().weekday() >= 5


def to_date(d: str) -> Date:
    return datetime.strptime(d, "%Y%m%d").date()
