"""交易所 JSON 共用解析工具。"""
from __future__ import annotations

import re

from ..dates import norm_date
from ..net import FetchError


def sign(s) -> int:
    t = re.sub(r"<[^>]+>", "", str(s or "")).strip()
    return -1 if "-" in t else (1 if "+" in t else 0)


def ok_stat(js) -> bool:
    stat = str(js.get("stat", "OK")) if isinstance(js, dict) else "OK"
    return stat.upper() == "OK"


def tables(js):
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


def dates_in(js) -> list[str]:
    found = []
    if isinstance(js, dict):
        for k in ("date", "reportDate"):
            if js.get(k):
                found.append(norm_date(js[k]))
        for t in js.get("tables", []) if isinstance(js.get("tables"), list) else []:
            if isinstance(t, dict) and t.get("date"):
                found.append(norm_date(t["date"]))
    return [f for f in found if f and re.fullmatch(r"\d{8}", f)]


def check_date(js, d: str, url: str = "") -> None:
    """回應裡有日期但不是 d 時丟出錯誤。
    回傳的日期比 d 新 → 網站沒照日期參數回應（網址或參數格式不對）→ parse 錯誤，會立刻啟用備援；
    比 d 舊 → 當日資料尚未公布 → nodata，稍後重抓。"""
    found = dates_in(js)
    if found and d not in found:
        newest = max(found)
        if newest > d:
            raise FetchError("parse", f"網站沒有依日期回傳（要求 {d}，回傳 {newest}），可能是網址參數不對", url)
        raise FetchError("nodata", f"{d} 尚未公布（網站目前最新為 {newest}）", url)


def col(fields, *must, exclude=()):
    """找出欄位名稱同時包含 must 所有字、且不含 exclude 任一字的欄位位置。"""
    if not fields:
        return None
    for i, f in enumerate(fields):
        s = str(f)
        if all(m in s for m in must) and not any(x in s for x in exclude):
            return i
    return None


def cell(row, i):
    return row[i] if i is not None and i < len(row) else None


CODE_RE = re.compile(r"[0-9A-Z]{4,6}")
