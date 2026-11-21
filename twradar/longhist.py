"""長期歷史：加權指數、0050、加權報酬指數（含息），用來驗證「抱 0050、大盤空頭時空手」這類只看大盤的做法。

和每日爬蟲分開存（long_px 表），一個月一個請求：
- 加權指數（OHLC）：{base}/indicesReport/MI_5MINS_HIST?date=YYYYMM01&response=json
- 0050 日成交：      {base}/afterTrading/STOCK_DAY?date=YYYYMM01&stockNo=0050&response=json
- 加權報酬指數：      {base}/TAIEX/MFI94U?date=YYYYMM01&response=json（含現金股利再投入，當 0050 含息報酬的替代）
"""
from __future__ import annotations

import math
from datetime import date

from .dates import norm_date
from .db import DB
from .net import FetchError, num
from .providers.common import ok_stat, sign, tables

BASE = "https://www.twse.com.tw/rwd/zh"
SERIES = {
    "taiex": ("加權指數", "indicesReport/MI_5MINS_HIST?date={d}&response=json"),
    "0050": ("0050", "afterTrading/STOCK_DAY?date={d}&stockNo=0050&response=json"),
    "tr": ("加權報酬指數", "TAIEX/MFI94U?date={d}&response=json"),
}
SCHEMA = """CREATE TABLE IF NOT EXISTS long_px (
  series TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, change REAL, PRIMARY KEY(series, date))"""


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    out = []
    while f"{y:04d}{m:02d}" <= end[:6]:
        out.append(f"{y:04d}{m:02d}01")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def parse(series: str, js) -> list[dict]:
    """解析一個月的回應。欄位依名稱對應，不依位置。"""
    if not ok_stat(js):
        return []
    out = []
    for _, fields, data in tables(js):
        if not fields or "日期" not in fields:
            continue
        ix = {f: i for i, f in enumerate(fields)}

        def col(*names):
            for n in names:
                if n in ix:
                    return ix[n]
            return None
        c_close = col("收盤指數", "收盤價", "發行量加權股價報酬指數")
        if c_close is None:
            continue
        c_open, c_high, c_low, c_chg = col("開盤指數", "開盤價"), col("最高指數", "最高價"), col("最低指數", "最低價"), col("漲跌價差")
        for r in data:
            d = norm_date(str(r[ix["日期"]]).strip())
            close = num(r[c_close])
            if not d or close is None:
                continue
            chg = None
            if c_chg is not None:
                raw = str(r[c_chg])
                v = num(raw.replace("X", ""))
                if v is None or ("X" in raw and v == 0):
                    chg = None            # 除權息日顯示「X0.00」時看不出參考價，不拿來還原
                else:
                    chg = abs(v) * (sign(raw) or 1) if v else 0.0
            out.append({"series": series, "date": d, "close": close,
                        "open": num(r[c_open]) if c_open is not None else None,
                        "high": num(r[c_high]) if c_high is not None else None,
                        "low": num(r[c_low]) if c_low is not None else None, "change": chg})
    return out


YAHOO = "https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?period1={a}&period2={b}&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
YAHOO_HOSTS = ("query2", "query1")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
YAHOO_SYMBOLS = {"taiex": "%5ETWII", "0050": "0050.TW"}


def parse_yahoo(series: str, js) -> list[dict]:
    """Yahoo 日線：0050 用還原股息與分割的收盤（adjclose），開高低也照同一比例還原；之後不必再做除權息還原。"""
    try:
        res = js["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        err = ((js or {}).get("chart") or {}).get("error") if isinstance(js, dict) else None
        raise FetchError("parse", f"Yahoo 回應格式不符：{err}")
    off = int((res.get("meta") or {}).get("gmtoffset") or 28800)
    q = (res.get("indicators") or {}).get("quote", [{}])[0]
    adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    from datetime import datetime, timezone
    out = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        close = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if close is None:
            continue
        k = (adj[i] / close) if series == "0050" and adj and i < len(adj) and adj[i] else 1.0
        d = datetime.fromtimestamp(ts + off, tz=timezone.utc).strftime("%Y%m%d")

        def g(f):
            v = (q.get(f) or [None] * (i + 1))[i] if i < len(q.get(f) or []) else None
            return None if v is None else round(v * k, 4)
        out[d] = {"series": series, "date": d, "open": g("open"), "high": g("high"), "low": g("low"),
                  "close": round(close * k, 4), "change": None}
    return [out[d] for d in sorted(out)]


def fetch_yahoo(db: DB, transport, start: str, end: str, series=("taiex", "0050"), log=print) -> dict:
    """從 Yahoo 一次抓整段（每個系列一個請求），避開證交所的請求次數限制。會覆蓋同系列已有的資料。"""
    from .dates import to_date
    ensure(db)
    from datetime import datetime, timezone
    a = int(datetime.combine(to_date(start), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    b = int(datetime.combine(to_date(end), datetime.min.time(), tzinfo=timezone.utc).timestamp()) + 86400
    stats = {}
    for s in series:
        if s not in YAHOO_SYMBOLS:
            log(f"Yahoo 沒有 {s}，略過")
            continue
        js, last = None, None
        for host in YAHOO_HOSTS:   # Yahoo 擋非瀏覽器的 User-Agent 會回 429；兩個主機輪流試
            try:
                js = transport.get_json(YAHOO.format(host=host, sym=YAHOO_SYMBOLS[s], a=a, b=b), retries=0)
                break
            except FetchError as e:
                last = e
                log(f"Yahoo {host}：{e}")
        if js is None:
            raise last
        rows = parse_yahoo(s, js)
        with db.tx() as c:
            c.execute("DELETE FROM long_px WHERE series=?", (s,))
            c.executemany("INSERT OR REPLACE INTO long_px(series,date,open,high,low,close,change) VALUES(?,?,?,?,?,?,?)",
                          [(r["series"], r["date"], r["open"], r["high"], r["low"], r["close"], r["change"]) for r in rows])
        stats[s] = {"fetched": 1, "skipped": 0, "failed": 0, "days": len(rows),
                    "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None}
    return stats


def ensure(db: DB) -> None:
    with db.tx() as c:
        c.execute(SCHEMA)


def fetch(db: DB, transport, start: str, end: str, series=("taiex", "0050", "tr"), base: str = BASE, log=print) -> dict:
    """逐月抓；已經抓過、而且不是最近兩個月的月份就跳過（可中斷續跑）。"""
    ensure(db)
    today = date.today().strftime("%Y%m%d")
    recent = months(today, today)[0]
    stats = {}
    for s in series:
        have = {r["m"] for r in db.q("SELECT DISTINCT substr(date,1,6) AS m FROM long_px WHERE series=?", (s,))}
        got = skip = fail = 0
        for m in months(start, end):
            if m[:6] in have and m[:6] < str(int(recent[:6]) - 1):
                skip += 1
                continue
            try:
                rows = parse(s, transport.get_json(f"{base}/{SERIES[s][1].format(d=m)}"))
            except FetchError as e:
                fail += 1
                log(f"{SERIES[s][0]} {m[:6]}：{e}")
                if e.kind == "blocked":   # 被證交所暫時封鎖：立刻停，繼續打只會封更久
                    stats[s] = {"fetched": got, "skipped": skip, "failed": fail, "blocked": True,
                                "days": db.one("SELECT COUNT(*) AS n FROM long_px WHERE series=?", (s,))["n"]}
                    stats["blocked"] = m[:6]
                    return stats
                continue
            with db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO long_px(series,date,open,high,low,close,change) VALUES(?,?,?,?,?,?,?)",
                              [(r["series"], r["date"], r["open"], r["high"], r["low"], r["close"], r["change"]) for r in rows])
            got += 1
            if got % 12 == 0:
                log(f"{SERIES[s][0]}：已抓到 {m[:6]}", flush=True) if log is print else log(f"{SERIES[s][0]}：已抓到 {m[:6]}")
        stats[s] = {"fetched": got, "skipped": skip, "failed": fail,
                    "days": db.one("SELECT COUNT(*) AS n FROM long_px WHERE series=?", (s,))["n"]}
    return stats


def load(db: DB, series: str) -> list[dict]:
    ensure(db)
    return [dict(r) for r in db.q("SELECT date, open, high, low, close, change FROM long_px WHERE series=? ORDER BY date", (series,))]


def adjusted(rows: list[dict]) -> list[dict]:
    """0050 的除息、分割還原（往回乘比例）：當天 收盤 − 漲跌 ≠ 前一天收盤 時，之前的價格乘上 (收盤 − 漲跌) ÷ 前一天收盤。"""
    out = [dict(r) for r in rows]
    for i in range(len(out) - 1, 0, -1):
        r, p = rows[i], rows[i - 1]
        if r["change"] is None or not p["close"]:
            continue
        ref = r["close"] - r["change"]
        if ref > 0 and abs(ref - p["close"]) > max(0.011, p["close"] * 0.001):
            k = ref / p["close"]
            if 0.05 <= k <= 20:
                for j in range(i):
                    for f in ("open", "high", "low", "close"):
                        if out[j][f] is not None:
                            out[j][f] *= k
    return out


# ---------------- 大盤進出規則 ----------------

def _ma(xs: list[float], i: int, n: int) -> float | None:
    return sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None


def bear_flags(taiex: list[dict]) -> dict[str, list[bool]]:
    """每一種規則每天「要不要空手」（收盤後判斷，隔天執行）。"""
    c = [r["close"] for r in taiex]
    n = len(c)
    ma20 = [_ma(c, i, 20) for i in range(n)]
    ma60 = [_ma(c, i, 60) for i in range(n)]

    def down(ma, i):
        return i > 0 and ma[i] is not None and ma[i - 1] is not None and ma[i] < ma[i - 1]

    def bear(i, tol=0.0, ma=ma20):
        return ma[i] is not None and c[i] < ma[i] * (1 - tol / 100) and down(ma, i)
    base = [bear(i) for i in range(n)]
    two, st = [], False
    for i in range(n):   # 連 2 日成立才空手、連 2 日不成立才買回
        if i and base[i] and base[i - 1]:
            st = True
        elif i and not base[i] and not base[i - 1]:
            st = False
        two.append(st)
    return {
        "空頭（跌破下彎月線）": base,
        "空頭連 2 日": two,
        "跌破月線 1% 且下彎": [bear(i, 1) for i in range(n)],
        "跌破月線 2% 且下彎": [bear(i, 2) for i in range(n)],
        "季線空頭": [bear(i, ma=ma60) for i in range(n)],
        "月線下彎": [down(ma20, i) for i in range(n)],
        "跌破月線": [ma20[i] is not None and c[i] < ma20[i] for i in range(n)],
        "非多頭（跌破月線或月線下彎）": [ma20[i] is not None and (c[i] < ma20[i] or down(ma20, i)) for i in range(n)],
    }


def simulate(asset: list[dict], dates: list[str], out_flag: list[bool], fee: float = 0.001425, tax: float = 0.001,
             use_open: bool = True) -> dict:
    """依 out_flag（以 dates 對齊）在隔一個交易日買賣 asset；回傳逐日資產（起點 1）與進出次數。"""
    px = {r["date"]: r for r in asset}
    cash, sh, want, last, curve, sw, held = 1.0, 0.0, True, None, [], 0, 0
    for i, d in enumerate(dates):
        b = px.get(d)
        if b and b["close"]:
            p = (b["open"] if use_open and b.get("open") else b["close"])
            if want and sh == 0:
                sh, cash = cash / (p * (1 + fee)), 0.0
                sw += 1
            elif not want and sh > 0:
                cash, sh = sh * p * (1 - fee - tax), 0.0
                sw += 1
            last = b["close"]
        if last is None:
            continue
        curve.append((d, cash + sh * last))
        held += sh > 0
        want = not out_flag[i]
    return {"curve": curve, "switches": sw, "exposure": round(held / len(curve) * 100, 1) if curve else None}


def stats(curve: list[tuple[str, float]]) -> dict:
    if len(curve) < 2:
        return {}
    from .dates import to_date
    v0, v1 = curve[0][1], curve[-1][1]
    yrs = max((to_date(curve[-1][0]) - to_date(curve[0][0])).days / 365.25, 1e-9)
    peak, dd = -math.inf, 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"total": round((v1 / v0 - 1) * 100, 1), "cagr": round(((v1 / v0) ** (1 / yrs) - 1) * 100, 2), "maxDD": round(dd * 100, 1)}


def by_year(curve: list[tuple[str, float]]) -> dict[str, float]:
    out, prev = {}, None
    ends = {}
    for d, v in curve:
        ends[d[:4]] = v
    first = curve[0][1] if curve else None
    for y in sorted(ends):
        base = prev if prev is not None else first
        out[y] = round((ends[y] / base - 1) * 100, 1)
        prev = ends[y]
    return out


def study(db: DB, start: str = "", asset: str = "0050") -> dict:
    """在長期歷史上比較各種大盤進出規則與一直抱著。asset：0050（還原除息、開盤成交）或 tr（加權報酬指數、收盤成交）。"""
    tx = load(db, "taiex")
    a = load(db, asset)
    if asset == "0050":
        a = adjusted(a)
    if start:
        tx = [r for r in tx if r["date"] >= start]
    common = sorted({r["date"] for r in tx} & {r["date"] for r in a})
    if len(common) < 100:
        return {"error": f"資料不足：加權指數 {len(tx)} 天、{asset} {len(a)} 天，重疊 {len(common)} 天"}
    tset = set(common)
    tx = [r for r in tx if r["date"] in tset]
    flags = bear_flags(tx)
    hold = simulate(a, common, [False] * len(common), use_open=asset == "0050")
    out = {"asset": asset, "start": common[0], "end": common[-1], "days": len(common),
           "rules": {"一直抱著": {**stats(hold["curve"]), "switches": 1, "exposure": 100.0, "years": by_year(hold["curve"])}}}
    for name, f in flags.items():
        s = simulate(a, common, f, use_open=asset == "0050")
        out["rules"][name] = {**stats(s["curve"]), "switches": s["switches"], "exposure": s["exposure"], "years": by_year(s["curve"])}
    return out
