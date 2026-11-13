"""臺灣集中保管結算所：集保戶股權分散表（每週）。

開放資料只提供最新一週的全市場 CSV（每週最後一個營業日收盤後更新），
欄位：資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存數比例%
持股分級 1–15 為持股級距、16 為差異數調整、17 為合計。
歷史資料要每週自己累積；系統每次抓取時檢查是否有新的一週。
"""
from __future__ import annotations

import csv
import io

from ..dates import norm_date
from ..net import FetchError, inum, num
from .base import Batch, Provider, register

# 各分級的下限（股）。第 15 級為 1,000,001 股以上
LEVEL_MIN_SHARES = [1, 1_000, 5_001, 10_001, 15_001, 20_001, 30_001, 40_001, 50_001, 100_001,
                    200_001, 400_001, 600_001, 800_001, 1_000_001]
LEVEL_MIN_LOTS = [0, 1, 5, 10, 15, 20, 30, 40, 50, 100, 200, 400, 600, 800, 1000]
URLS = ["{b}/opendata/getOD.ashx?id=1-5"]
ALT_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"  # 舊網址，只在使用預設網址時備用


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_csv(text: str, codes: set[str] | None = None) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise FetchError("parse", "集保 CSV 是空的")
    head = [h.strip() for h in rows[0]]

    def ix(*keys):
        for i, h in enumerate(head):
            if all(k in h for k in keys):
                return i
        return None
    i_d, i_c, i_l = ix("日期"), ix("代號"), ix("分級")
    i_p, i_s, i_r = ix("人數"), ix("股數"), ix("比例")
    if None in (i_d, i_c, i_l, i_p, i_s):
        raise FetchError("parse", f"集保 CSV 欄位無法辨識：{head}")
    out = []
    for r in rows[1:]:
        if len(r) <= max(i_d, i_c, i_l, i_p, i_s):
            continue
        code = r[i_c].strip()
        if codes is not None and code not in codes:
            continue
        lv = inum(r[i_l])
        if lv is None or not (1 <= lv <= 15 or lv == 17):
            continue
        out.append({"date": norm_date(r[i_d]), "code": code, "level": lv, "people": inum(r[i_p]),
                    "shares": inum(r[i_s]), "pct": num(r[i_r]) if i_r is not None else None, "src": "TDCC 1-5"})
    if not out and codes is None:
        raise FetchError("parse", "集保 CSV 沒有資料列")
    return out


@register
class TDCC(Provider):
    id = "tdcc"
    label = "集保"
    scope = "weekly"
    datasets = ("holders",)
    base = "https://smart.tdcc.com.tw"

    def fetch_week(self, codes: set[str] | None = None) -> Batch:
        """最新一週的股權分散表；codes 指定時只保留這些股票。"""
        err = None
        urls = [u.format(b=self.base) for u in URLS] + ([ALT_URL] if self.base == TDCC.base else [])
        for url in urls:
            try:
                return {"holders": parse_csv(decode(self.t.get(url, retries=0)), codes)}
            except FetchError as e:
                err = e
        raise err or FetchError("nodata", "集保股權分散表無法取得")

    def doctor(self, d: str):
        def run():
            rows = self.fetch_week({"2330"})["holders"]
            return f"資料日 {rows[0]['date']}，台積電 {len(rows)} 列" if rows else "沒有台積電資料"
        return [("集保股權分散表（週）", run)]
