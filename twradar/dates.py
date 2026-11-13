"""日期工具。內部一律使用 'YYYYMMDD' 字串。"""
from __future__ import annotations

import re
from datetime import date as Date, datetime, timedelta


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


def roc(d: str) -> str:
    return f"{int(d[:4]) - 1911}/{d[4:6]}/{d[6:8]}"


def slash(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:8]}"


def dash(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def to_date(d: str) -> Date:
    return datetime.strptime(d, "%Y%m%d").date()


def is_weekend(d: str) -> bool:
    return to_date(d).weekday() >= 5


def weekdays_back(end: str, n: int) -> list[str]:
    """從 end（含）往回 n 個平日（不判斷國定假日）。"""
    d = to_date(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def prev_weekday(d: str) -> str:
    x = to_date(d) - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x.strftime("%Y%m%d")
