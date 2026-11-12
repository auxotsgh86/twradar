"""HTTP 抓取：同一網站請求自動間隔、重試、封鎖偵測。只用標準函式庫。"""
from __future__ import annotations

import gzip
import json
import logging
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("twradar.net")


class FetchError(Exception):
    """kind: blocked（被網站擋）、http、network、parse、nodata（該日沒有資料）"""

    def __init__(self, kind: str, message: str, url: str = ""):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.url = url


def _ssl_context() -> ssl.SSLContext:
    try:  # macOS 上的 Python 常缺根憑證；有 certifi 就用它
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Fetcher:
    def __init__(self, interval: float = 8.0, user_agent: str = "twradar", timeout: float = 45.0):
        self.interval = interval
        self.ua = user_agent
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ctx = _ssl_context()
        self.requests = 0

    def _wait(self, host: str) -> None:
        with self._lock:
            last = self._last.get(host, 0.0)
            gap = self.interval + random.uniform(0, self.interval * 0.25)
            delay = last + gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()

    def get(self, url: str, retries: int = 1) -> bytes:
        host = urlparse(url).netloc
        attempt = 0
        while True:
            self._wait(host)
            self.requests += 1
            req = urllib.request.Request(url, headers={
                "User-Agent": self.ua, "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip", "Accept-Language": "zh-TW,zh;q=0.9"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body
            except urllib.error.HTTPError as e:
                kind = "blocked" if e.code in (403, 429) else "http"
                err = FetchError(kind, f"HTTP {e.code}", url)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                err = FetchError("network", str(e), url)
            attempt += 1
            if attempt > retries:
                raise err
            log.warning("%s，%d 秒後重試：%s", err, 60, url)
            time.sleep(60)

    def get_json(self, url: str, retries: int = 1):
        body = self.get(url, retries)
        text = body.decode("utf-8", errors="replace").lstrip("﻿").strip()
        if not text.startswith(("{", "[")):
            low = text[:3000].lower()
            if "security" in low or "安全" in low or "<html" in low:
                raise FetchError("blocked", "網站回傳封鎖頁面或 HTML，不是 JSON", url)
            raise FetchError("parse", "回應不是 JSON", url)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError("parse", f"JSON 解析失敗：{e}", url)


def num(v):
    """把交易所字串數字轉成 float；'--'、''、'X' 等回傳 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("　", "")
    if s.startswith("<"):
        import re
        s = re.sub(r"<[^>]+>", "", s).strip()
    if s in ("", "--", "---", "X", "除權息", "除權", "除息", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def inum(v):
    n = num(v)
    return None if n is None else int(round(n))
