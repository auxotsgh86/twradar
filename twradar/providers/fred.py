"""10 年期美債殖利率（%）：先試美國聯準會 FRED（DGS10，免金鑰 CSV），連不上再改用 Yahoo（^TNX）。

https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd=YYYY-MM-DD&coed=YYYY-MM-DD
回傳兩欄（observation_date 或 DATE, DGS10）；休市日的值是「.」或空白。
日期是美國的交易日：美股收盤是台灣清晨，所以判定台股 d 日時只能用「美國 d 之前」的值（見 macro.Macro）。
"""
from __future__ import annotations

import csv
import io

from ..net import FetchError

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={a}&coed={b}"


def _dash(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def parse(body: bytes) -> list[dict]:
    text = body.decode("utf-8-sig", errors="replace")
    if text.lstrip()[:1] == "<":
        raise FetchError("parse", "FRED 回應是 HTML，不是 CSV")
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if not rows or len(rows[0]) < 2:
        return []
    out = []
    for r in rows[1:]:
        d, v = r[0].strip(), r[1].strip()
        if len(d) != 10 or d[4] != "-":
            continue
        try:
            x = float(v)
        except ValueError:
            continue
        out.append({"date": d.replace("-", ""), "ust10y": x})
    return out


class Fred:
    def __init__(self, transport, url: str = URL):
        self.t, self.url = transport, url

    def ust10y(self, start: str, end: str) -> list[dict]:
        return parse(self.t.get(self.url.format(sid="DGS10", a=_dash(start), b=_dash(end))))


YAHOO_TNX = "https://{host}.finance.yahoo.com/v8/finance/chart/%5ETNX?period1={a}&period2={b}&interval=1d"


def parse_tnx(js) -> list[dict]:
    """Yahoo ^TNX 日線 → [{date（美國交易日）, ust10y（%）}]。"""
    from datetime import datetime, timezone
    try:
        res = js["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise FetchError("parse", "Yahoo ^TNX 回應格式不符")
    off = int((res.get("meta") or {}).get("gmtoffset") or -18000)
    close = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for ts, v in zip(res.get("timestamp") or [], close):
        if v is None:
            continue
        out[datetime.fromtimestamp(ts + off, tz=timezone.utc).strftime("%Y%m%d")] = v
    vals = sorted(out.values())
    scale = 0.1 if vals and vals[len(vals) // 2] > 20 else 1.0     # 舊式報價是殖利率 ×10
    return [{"date": d, "ust10y": round(out[d] * scale, 4)} for d in sorted(out)]


def fetch_ust10y(start: str, end: str, transport=None, log=print) -> tuple[list[dict], str]:
    """先 FRED、再 Yahoo ^TNX。回傳 (rows, 來源)。兩者都失敗時丟出最後的錯誤。"""
    from datetime import datetime, timezone

    from ..dates import to_date
    from ..longhist import BROWSER_UA, YAHOO_HOSTS
    from ..net import Fetcher
    err = None
    try:
        rows = Fred(transport or Fetcher(1.0, BROWSER_UA, timeout=20)).ust10y(start, end)
        if rows:
            return rows, "FRED"
        err = FetchError("parse", "FRED 沒有資料")
    except FetchError as e:
        err = e
        log(f"FRED 連不上（{e}），改用 Yahoo ^TNX")
    y = Fetcher(1.0, BROWSER_UA, timeout=30)
    a = int(datetime.combine(to_date(start), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    b = int(datetime.combine(to_date(end), datetime.min.time(), tzinfo=timezone.utc).timestamp()) + 86400
    for host in YAHOO_HOSTS:
        try:
            rows = parse_tnx(y.get_json(YAHOO_TNX.format(host=host, a=a, b=b), retries=0))
            if rows:
                return rows, "Yahoo ^TNX"
        except FetchError as e:
            err = e
            log(f"Yahoo {host}：{e}")
    raise err
