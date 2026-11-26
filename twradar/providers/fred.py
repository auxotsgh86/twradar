"""美國聯準會 FRED：10 年期美債殖利率（DGS10，%）。免金鑰 CSV。

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
