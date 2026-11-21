"""期交所：三大法人臺股期貨未平倉（外資淨口數）、每日外幣參考匯率（美元兌新台幣）。

下載端點（表單 POST，回傳 Big5 CSV，可一次查一段日期）：
- {base}/futContractsDateDown  queryStartDate=YYYY/MM/DD&queryEndDate=…&commodityId=TXF
- {base}/dailyFXRateDown       queryStartDate=…&queryEndDate=…
"""
from __future__ import annotations

import csv
import io

from ..net import FetchError, num

BASE = "https://www.taifex.com.tw/cht/3"
ROLES = {"外資及陸資": "fut_foreign", "外資": "fut_foreign", "投信": "fut_trust", "自營商": "fut_dealer"}


def _slash(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:8]}"


def _date(s: str) -> str | None:
    s = (s or "").strip().replace("-", "/")
    p = s.split("/")
    if len(p) == 3 and all(x.isdigit() for x in p):
        return f"{int(p[0]):04d}{int(p[1]):02d}{int(p[2]):02d}"
    if len(s) == 8 and s.isdigit():
        return s
    return None


def _rows(body: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            text = body.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = body.decode("cp950", errors="replace")
    if text.lstrip()[:1] == "<":
        low = text[:3000].lower()
        if "查無資料" in text or "no data" in low:
            return []
        raise FetchError("parse", "回應是 HTML，不是 CSV")
    return [r for r in csv.reader(io.StringIO(text)) if r and any(x.strip() for x in r)]


def parse_futures(body: bytes) -> list[dict]:
    """→ [{date, fut_foreign, fut_trust, fut_dealer}]：臺股期貨各身分別的多空未平倉口數淨額。"""
    rows = _rows(body)
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    try:
        idate, iname, irole = head.index("日期"), head.index("商品名稱"), head.index("身份別")
        inet = head.index("多空未平倉口數淨額")
    except ValueError:
        raise FetchError("parse", f"期貨 CSV 欄位不符：{head[:6]}")
    out: dict[str, dict] = {}
    for r in rows[1:]:
        if len(r) <= max(idate, iname, irole, inet):
            continue
        d, role = _date(r[idate]), ROLES.get(r[irole].strip())
        if not d or not role or "臺股期貨" not in r[iname] and "台股期貨" not in r[iname]:
            continue
        v = num(r[inet])
        if v is not None:
            out.setdefault(d, {"date": d, "src": "TAIFEX"})[role] = int(v)
    return sorted(out.values(), key=lambda x: x["date"])


def parse_fx(body: bytes) -> list[dict]:
    """→ [{date, usd_twd}]"""
    rows = _rows(body)
    if not rows:
        return []
    head = [h.strip().replace("／", "/") for h in rows[0]]
    try:
        idate, iusd = head.index("日期"), head.index("美元/新台幣")
    except ValueError:
        raise FetchError("parse", f"匯率 CSV 欄位不符：{head[:4]}")
    out = []
    for r in rows[1:]:
        if len(r) <= max(idate, iusd):
            continue
        d, v = _date(r[idate]), num(r[iusd])
        if d and v:
            out.append({"date": d, "usd_twd": v, "src": "TAIFEX"})
    return out


class Taifex:
    def __init__(self, transport, base: str = BASE):
        self.t, self.base = transport, base.rstrip("/")

    def futures(self, start: str, end: str) -> list[dict]:
        return parse_futures(self.t.post(f"{self.base}/futContractsDateDown", {
            "queryStartDate": _slash(start), "queryEndDate": _slash(end), "commodityId": "TXF"}))

    def fx(self, start: str, end: str) -> list[dict]:
        return parse_fx(self.t.post(f"{self.base}/dailyFXRateDown", {
            "queryStartDate": _slash(start), "queryEndDate": _slash(end)}))

    def fetch(self, start: str, end: str) -> tuple[list[dict], list[str]]:
        """回傳（資料列, 失敗訊息）；期貨與匯率各自獨立。"""
        rows, errs = [], []
        for label, fn in (("期交所三大法人期貨", self.futures), ("期交所外幣參考匯率", self.fx)):
            try:
                rows += fn(start, end)
            except FetchError as e:
                errs.append(f"{label}：{e}")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{label}：格式無法解析（{e!r}）")
        return rows, errs
