"""注意股、處置股（證交所、櫃買中心公告）。欄位格式已依 2026/11 實際回應確認。

- 證交所注意：announcement/notice   欄位：編號, 證券代號, 證券名稱, 累計次數, 注意交易資訊, 日期, 收盤價, 本益比
  （日期格式是「115.11.13」；查詢要帶 querytype=1 等參數，否則回傳空資料）
- 證交所處置：announcement/punish   欄位：編號, 公布日期, 證券代號, 證券名稱, 累計, 處置條件, 處置起迄時間, 處置措施, 處置內容, 備註
- 櫃買注意：bulletin/attention       tables[0] 欄位：編號, 證券代號, 證券名稱, 累計, 注意交易資訊, 公告日期, 收盤價, 本益比, link
- 櫃買處置：bulletin/disposal        tables[0] 欄位：編號, 公布日期, 證券代號, 證券名稱, 累計, 處置起訖時間, 處置原因, 處置措施, 處置內容, …
  （沒有處置的日子會有「本日無處置資料」的空列；證券名稱後面可能帶連結，例如「霖宏二(../../…)」）
日期為民國年（115/11/10 或 115.11.13）。處置期間例如「115/11/11～115/11/17」或「115/11/12~115/11/18」。
"""
from __future__ import annotations

import re

from ..dates import norm_date, slash
from ..net import FetchError, inum
from .common import CODE_RE

TWSE = "https://www.twse.com.tw/announcement"
TPEX = "https://www.tpex.org.tw/www/zh-tw/bulletin"


def _roc(s) -> str | None:
    """民國年 '115/11/10'、'115.11.13' → 'YYYYMMDD'；格式不對回傳 None。"""
    m = re.fullmatch(r"\s*(\d{2,3})[/.\-](\d{1,2})[/.\-](\d{1,2})\s*", str(s or ""))
    if not m:
        return None
    return f"{int(m[1]) + 1911:04d}{int(m[2]):02d}{int(m[3]):02d}"


def _period(s) -> tuple[str | None, str | None]:
    parts = re.split(r"\s*[～~]\s*", str(s or "").strip())
    return (_roc(parts[0]), _roc(parts[1])) if len(parts) == 2 else (None, None)


def _name(s) -> str:
    return re.sub(r"\(.*$", "", str(s or "")).strip()


def _text(s, n: int = 160) -> str:
    t = re.sub(r"<br\s*/?>", " ", str(s or ""))
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def _match_every(s) -> str:
    """處置內容裡的撮合頻率，例如「約每二十分鐘撮合一次」→ '每二十分鐘撮合'。"""
    m = re.search(r"每\s*([一二三四五六七八九十0-9]{1,4})\s*分鐘\s*撮合", str(s or ""))
    return f"每{m[1]}分鐘撮合" if m else ""


def _ix(fields, *names):
    fs = [str(f).strip() for f in fields]
    for n in names:
        for i, f in enumerate(fs):
            if f.startswith(n):
                return i
    return None


def _row(kind, market, code, name, date, count, reason, start=None, end=None, measure="", src="") -> dict | None:
    code = str(code or "").strip()
    if not CODE_RE.fullmatch(code) or not date:
        return None
    return {"date": date, "code": code, "kind": kind, "market": market, "name": name, "count": count,
            "reason": reason, "start": start, "end": end, "measure": measure, "src": src}


def parse_twse_notice(js) -> list[dict]:
    if not isinstance(js, dict) or js.get("stat") != "OK":
        raise FetchError("parse", f"證交所注意股回應異常：{str(js)[:120]}")
    f = js.get("fields") or []
    ic, iname, icnt, iwhy, idate = (_ix(f, "證券代號"), _ix(f, "證券名稱"), _ix(f, "累計"), _ix(f, "注意交易資訊"), _ix(f, "日期"))
    if None in (ic, iname, iwhy, idate):
        raise FetchError("parse", f"證交所注意股欄位改變：{f}")
    out, bad = [], 0
    for r in js.get("data") or []:
        if not _roc(r[idate]):
            bad += 1
        x = _row("注意", "TWSE", r[ic], _name(r[iname]), _roc(r[idate]), inum(r[icnt]) if icnt is not None else None,
                 _text(r[iwhy]), src="TWSE notice")
        if x:
            out.append(x)
    if bad and not out:
        raise FetchError("parse", f"證交所注意股日期格式無法解析（{bad} 筆）")
    return out


def parse_twse_punish(js) -> list[dict]:
    if not isinstance(js, dict) or js.get("stat") != "OK":
        raise FetchError("parse", f"證交所處置股回應異常：{str(js)[:120]}")
    f = js.get("fields") or []
    ic, iname, idate, icnt, icond, iper, imeas, icont = (_ix(f, "證券代號"), _ix(f, "證券名稱"), _ix(f, "公布日期"), _ix(f, "累計"),
                                                         _ix(f, "處置條件"), _ix(f, "處置起迄", "處置起訖"), _ix(f, "處置措施"),
                                                         _ix(f, "處置內容"))
    if None in (ic, iname, idate, iper):
        raise FetchError("parse", f"證交所處置股欄位改變：{f}")
    out = []
    for r in js.get("data") or []:
        start, end = _period(r[iper])
        why = "；".join(x for x in (_text(r[icond]) if icond is not None else "", _text(r[imeas]) if imeas is not None else "") if x)
        x = _row("處置", "TWSE", r[ic], _name(r[iname]), _roc(r[idate]), inum(r[icnt]) if icnt is not None else None, why,
                 start, end, _match_every(r[icont]) if icont is not None else "", "TWSE punish")
        if x:
            out.append(x)
    return out


def _tpex_table(js, what):
    tabs = js.get("tables") if isinstance(js, dict) else None
    if not tabs:
        raise FetchError("parse", f"櫃買{what}回應異常：{str(js)[:120]}")
    return tabs[0].get("fields") or [], tabs[0].get("data") or []


def parse_tpex_attention(js) -> list[dict]:
    f, data = _tpex_table(js, "注意股")
    ic, iname, icnt, iwhy, idate = (_ix(f, "證券代號"), _ix(f, "證券名稱"), _ix(f, "累計"), _ix(f, "注意交易資訊"), _ix(f, "公告日期"))
    if None in (ic, iname, iwhy, idate):
        raise FetchError("parse", f"櫃買注意股欄位改變：{f}")
    out = []
    for r in data:
        x = _row("注意", "TPEx", r[ic], _name(r[iname]), _roc(r[idate]), inum(r[icnt]) if icnt is not None else None,
                 _text(r[iwhy]), src="TPEx attention")
        if x:
            out.append(x)
    return out


def parse_tpex_disposal(js) -> list[dict]:
    f, data = _tpex_table(js, "處置股")
    ic, iname, idate, icnt, iper, iwhy, imeas, icont = (_ix(f, "證券代號"), _ix(f, "證券名稱"), _ix(f, "公布日期"), _ix(f, "累計"),
                                                        _ix(f, "處置起訖", "處置起迄"), _ix(f, "處置原因"), _ix(f, "處置措施"),
                                                        _ix(f, "處置內容"))
    if None in (ic, iname, idate, iper):
        raise FetchError("parse", f"櫃買處置股欄位改變：{f}")
    out = []
    for r in data:
        start, end = _period(r[iper])
        why = "；".join(x for x in (_text(r[iwhy]) if iwhy is not None else "", _text(r[imeas]) if imeas is not None else "") if x)
        x = _row("處置", "TPEx", r[ic], _name(r[iname]), _roc(r[idate]), inum(r[icnt]) if icnt is not None else None, why,
                 start, end, _match_every(r[icont]) if icont is not None else "", "TPEx disposal")
        if x:
            out.append(x)
    return out


class Alerts:
    """抓取 start～end（YYYYMMDD）之間公布的注意股與處置股。"""

    def __init__(self, transport, twse: str = TWSE, tpex: str = TPEX):
        self.t, self.twse, self.tpex = transport, twse.rstrip("/"), tpex.rstrip("/")

    def urls(self, start: str, end: str) -> list[tuple[str, str, callable]]:
        TWSE, TPEX = self.twse, self.tpex  # noqa: N806
        return [
            ("證交所注意股", f"{TWSE}/notice?response=json&startDate={start}&endDate={end}&querytype=1&stockNo=&selectType=&sortKind=STKNO",
             parse_twse_notice),
            ("證交所處置股", f"{TWSE}/punish?response=json&startDate={start}&endDate={end}",
             parse_twse_punish),
            ("櫃買注意股", f"{TPEX}/attention?startDate={slash(start)}&endDate={slash(end)}&response=json", parse_tpex_attention),
            ("櫃買處置股", f"{TPEX}/disposal?startDate={slash(start)}&endDate={slash(end)}&response=json", parse_tpex_disposal),
        ]

    def fetch(self, start: str, end: str) -> tuple[list[dict], list[str]]:
        """回傳（資料列, 失敗訊息）。某個來源失敗不影響其他來源。"""
        rows, errs = [], []
        for label, url, parse in self.urls(norm_date(start), norm_date(end)):
            try:
                rows += parse(self.t.get_json(url))
            except FetchError as e:
                errs.append(f"{label}：{e}")
            except Exception as e:  # noqa: BLE001  格式改版
                errs.append(f"{label}：格式無法解析（{e!r}）")
        return rows, errs
