"""集保戶股權分散表的歷史（集保結算所查詢頁，約一年、每週一筆、一次查一檔一週）。

格式（2026/11 實際回應確認）
- GET 查詢頁：表單欄位 SYNCHRONIZER_TOKEN、SYNCHRONIZER_URI、method=submit、firDate、scaDate、sqlMethod、stockNo、stockName；
  scaDate 的選項就是可查的週別（約 51 週）。
- POST 後回應同一頁加上表格；每次回應都帶新的 token，接著查下一筆直接用（token 用過就失效）。
- 表格每列：持股分級、級距、人數、股數、占集保庫存數比例%。
  新的週別：16＝差異數調整、17＝合 計；較舊的週別沒有差異數調整，16 就是合 計 → 以「合 計」這列為準，存成 17。
- 查不到：表格內出現「查無此資料」（例如當時還沒上市）。
"""
from __future__ import annotations

import http.cookiejar
import re
import time
import urllib.parse
import urllib.request

from ..net import FetchError, inum, num

URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"


def form_of(html: str) -> dict[str, str]:
    f: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html):
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if n:
            f[n[1]] = v[1] if v else ""
    return f


def weeks_of(html: str) -> list[str]:
    return re.findall(r'<option value="(\d{8})"', html)


def _cells(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)]


def parse(html: str, code: str, date: str) -> list[dict] | None:
    """回傳 1～15 級與合計（17）；查無資料回傳 None；格式不對丟 FetchError。"""
    cells = _cells(html)
    if "查無此資料" in cells:
        return None
    out, total = [], None
    for i in range(len(cells) - 4):
        lv = cells[i]
        if not lv.isdigit():
            continue
        label = cells[i + 1].replace(" ", "")
        lvi = int(lv)
        if label == "合計":
            total = {"level": 17, "people": inum(cells[i + 2]), "shares": inum(cells[i + 3]), "pct": num(cells[i + 4])}
        elif "差異" in label:
            continue
        elif 1 <= lvi <= 15 and re.search(r"\d", label) and len(out) == lvi - 1:
            out.append({"level": lvi, "people": inum(cells[i + 2]), "shares": inum(cells[i + 3]), "pct": num(cells[i + 4])})
    if len(out) != 15 or total is None:
        raise FetchError("parse", f"集保查詢頁格式無法解析（{code} {date}：{len(out)} 級、合計 {'有' if total else '無'}）")
    return [{**r, "date": date, "code": code, "src": "TDCC 查詢"} for r in out + [total]]


class Session:
    """維持 cookie 與 token；open_fn 可替換（測試用）。"""

    def __init__(self, user_agent: str = "Mozilla/5.0", interval: float = 3.0, open_fn=None, sleep=time.sleep):
        self.interval, self.sleep, self.last = interval, sleep, 0.0
        if open_fn is None:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            op.addheaders = [("User-Agent", user_agent)]
            open_fn = lambda url, data=None: op.open(url, data, timeout=30).read().decode("utf-8", "replace")  # noqa: E731
        self.open = open_fn
        self.form: dict[str, str] = {}
        self.weeks: list[str] = []

    def _wait(self):
        dt = self.interval - (time.monotonic() - self.last)
        if dt > 0:
            self.sleep(dt)
        self.last = time.monotonic()

    def start(self) -> list[str]:
        self._wait()
        html = self.open(URL)
        self.form, self.weeks = form_of(html), weeks_of(html)
        if "SYNCHRONIZER_TOKEN" not in self.form or not self.weeks:
            raise FetchError("parse", "集保查詢頁沒有表單或週別")
        return self.weeks

    def query(self, code: str, date: str) -> list[dict] | None:
        """查一檔一週；token 失效或格式不對時重新整理一次再試；連線錯誤等 30 秒後重試，最多 3 次。"""
        last_err = None
        for attempt in range(3):
            try:
                if not self.form:
                    self.start()
                self._wait()
                body = urllib.parse.urlencode({**self.form, "scaDate": date, "sqlMethod": "StockNo", "stockNo": code,
                                               "stockName": ""}).encode()
                html = self.open(URL, body)
                f = form_of(html)
                if f.get("SYNCHRONIZER_TOKEN"):
                    self.form = f
                try:
                    return parse(html, code, date)
                except FetchError as e:
                    last_err = e
                    self.form = {}          # 重新取得 token 再試
                    continue
            except FetchError:
                raise
            except Exception as e:  # noqa: BLE001  連線問題
                last_err = e
                self.form = {}
                self.sleep(30)
        raise FetchError("http", f"集保查詢失敗（{code} {date}）：{last_err!r}")
