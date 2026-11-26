"""逐筆成交的每日彙總（主動買賣、大單）：收集與查詢。資料來源：永豐 Shioaji（providers/sinopac.py）。

每檔每天存一列（張）：vol 成交量、buy 外盤（買方主動）、sell 內盤（賣方主動）、unk 無法判斷、
bigBuy／bigSell 單筆 ≥ 500 萬元的主動買／賣、hugeBuy／hugeSell ≥ 2000 萬元（開盤、收盤集合競價不算主動）、amount 成交金額（元）、vwap 均價。
只存彙總、不存原始 ticks（一天一檔可能上萬筆）。先累積資料，規則還沒用到。
"""
from __future__ import annotations

import logging
from typing import Callable

from .db import DB
from .providers.sinopac import aggregate

log = logging.getLogger("twradar.tickflow")

COLS = ("n", "vol", "buy", "sell", "unk", "bigBuy", "bigSell", "hugeBuy", "hugeSell", "amount", "vwap")
SRC = "sinopac2"   # 彙總定義版本：v2＝大單 500 萬／2000 萬、集合競價不算主動（舊的 sinopac 會重抓）
MIN_REMAIN_MB = 15  # 官方剩餘流量低於這個就停
EMPTY_STOP = 3     # 連續幾檔「有成交卻拿到 0 筆」→ 判定今天的流量用完，停止


def done(db: DB, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    have = set()
    codes = sorted({c for c, _ in pairs})
    for i in range(0, len(codes), 500):
        ch = codes[i:i + 500]
        have |= {(r["code"], r["date"]) for r in db.q(f"SELECT code, date FROM tickflow WHERE src IN (?, 'nocontract') "
                                                       f"AND code IN ({','.join('?' * len(ch))})", (SRC, *ch))}
    return have


def collect(db: DB, client, pairs: list[tuple[str, str]], budget_mb: float = 450,
            progress: Callable[[str], None] | None = None, reserve_mb: float = MIN_REMAIN_MB) -> dict:
    """依序抓 (代號, 日期) 的 ticks、彙總後存檔；已有的跳過。流量估計超過 budget_mb、或判定流量用完時停止。"""
    have = done(db, pairs)
    todo = [p for p in pairs if p not in have]
    vols = {}
    if todo:
        codes = sorted({c for c, _ in todo})
        for i in range(0, len(codes), 500):
            ch = codes[i:i + 500]
            for r in db.q(f"SELECT code, date, volume FROM quotes WHERE code IN ({','.join('?' * len(ch))})", ch):
                vols[(r["code"], r["date"])] = r["volume"]
    st = {"todo": len(todo), "saved": 0, "skipped": len(pairs) - len(todo), "stopped": "", "mb": 0.0}
    empties = 0
    for k, (code, d) in enumerate(todo):
        if client.bytes / 1e6 >= budget_mb:
            st["stopped"] = f"已達預估流量上限 {budget_mb:g} MB"
            break
        if k % 25 == 0:
            u = client.usage() if hasattr(client, "usage") else None
            if u and u.get("remaining") is not None and u["remaining"] / 1e6 < reserve_mb:
                st["stopped"] = f"官方剩餘流量 {u['remaining'] / 1e6:.0f} MB，低於保留的 {reserve_mb:g} MB（每個交易日 08:00 重置）"
                break
        traded = (vols.get((code, d)) or 0) > 0
        try:
            t = client.ticks(code, d)
        except Exception as e:  # noqa: BLE001
            st["stopped"] = f"{code} {d} 查詢失敗：{e!r}"
            break
        if t is None:
            row, src = {c: None for c in COLS}, "nocontract"
        else:
            row, src = aggregate(t), SRC
            if row["n"] == 0 and traded:
                empties += 1
                if empties >= EMPTY_STOP:
                    st["stopped"] = "連續拿到空資料：今天的流量可能已用完（每個交易日 08:00 重置）"
                    break
                continue          # 不存：之後再試
            empties = 0
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO tickflow(code,date,n,vol,buy,sell,unk,big_buy,big_sell,huge_buy,huge_sell,amount,vwap,src) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (code, d, *(row[c] for c in COLS), src))
        st["saved"] += 1
        if progress and (k + 1) % 50 == 0:
            progress(f"{k + 1}/{len(todo)}（{code} {d}）約 {client.bytes / 1e6:.0f} MB")
    st["mb"] = round(client.bytes / 1e6, 1)
    u = client.usage() if hasattr(client, "usage") else None
    if u and u.get("bytes") is not None:
        st["usedMb"] = round(u["bytes"] / 1e6, 1)
        st["limitMb"] = round(u["limit"] / 1e6) if u.get("limit") else None
    st["calls"] = client.calls
    return st


BACKFILL_RESERVE_MB = 120    # 自動補抓時保留給收盤後每日收集的流量


def backfill_pairs(db: DB, S: dict, start: str = "20241001", end: str = "99999999", universe: str = "watchlist,大型",
                   extra: str = "") -> tuple[list[tuple[str, str]], int, int]:
    """補抓清單：新的日子先抓。回傳 (pairs, 檔數, 天數)。"""
    from .backtest import BTConfig, rank_universe
    days = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=? ORDER BY date DESC",
                                    (start, end))]
    if not days:
        return [], 0, 0
    want = set(universe.split(","))
    codes = set()
    if "watchlist" in want:
        codes |= {w["code"] for w in S.get("watchlist") or []}
    alias = {"large": "大型", "mid": "中型", "otc": "上櫃"}
    tags = {alias.get(x, x) for x in want} & {"大型", "中型", "上櫃"}
    if tags:
        u = rank_universe(db, days[0], BTConfig("", "", ""))
        codes |= {c for c, t in u.items() if t & tags}
    if extra:
        codes |= set(extra.split(","))
    return [(c, d) for d in days for c in sorted(codes)], len(codes), len(days)


def coverage(db: DB, S: dict, start: str = "20241001", universe: str = "watchlist,大型") -> dict:
    pairs, nc, nd = backfill_pairs(db, S, start, universe=universe)
    have = done(db, pairs)
    got = [d for c, d in pairs if (c, d) in have]
    return {"stocks": nc, "days": nd, "total": len(pairs), "done": len(have), "left": len(pairs) - len(have),
            "from": min(got) if got else None, "to": max(got) if got else None,
            "rows": db.one("SELECT COUNT(*) AS n FROM tickflow WHERE src=?", (SRC,))["n"]}


def daily_targets(db: DB, S: dict, d: str) -> list[str]:
    """每天收的股票：觀察清單、實盤追蹤持有中與委託中、當天亮上車燈的股票。"""
    codes = {w["code"] for w in S.get("watchlist") or []}
    codes |= {r["code"] for r in db.q("SELECT code FROM paper_pos UNION SELECT code FROM paper_order")}
    codes |= {r["code"] for r in db.q("SELECT code FROM signals WHERE date=? AND light='go'", (d,))}
    return sorted(codes)


def series(db: DB, code: str, days: int = 60) -> list[dict]:
    rows = db.q("SELECT * FROM tickflow WHERE code=? AND src=? ORDER BY date DESC LIMIT ?", (code, SRC, days))
    out = []
    for r in reversed(rows):
        r = dict(r)
        bs = (r["buy"] or 0) + (r["sell"] or 0)
        r["buyPct"] = round(r["buy"] / bs * 100, 1) if bs else None                       # 主動買占（外＋內）%
        r["bigNetPct"] = round(((r["big_buy"] or 0) - (r["big_sell"] or 0)) / r["vol"] * 100, 2) if r["vol"] else None
        out.append(r)
    return out
