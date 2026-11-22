"""實盤追蹤（紙上投資組合）：從上線那天起，每天收盤後照目前的規則下單，事後不能改。

為什麼要有這個
- 回溯分析再怎麼小心，規則都是看過那段歷史才定的；只有「上線之後」的成績完全沒有被調參數污染。
- 這裡記下的每一筆都是當天收盤時就決定、隔天開盤成交，之後不會重算、不會因為規則改版而改寫。

做法（和回溯分析的投資組合模擬相同）
- 資金分成 10／20／30 份三本帳，各自獨立；起始資產都是 1。
- 每天收盤後（判定日 d，只用 d 以前的資料）：
  1. 持有中的股票照目前規則判定，出現「出場」→ 隔天開盤賣出（不做減碼與加碼，每檔一次買進、一次賣出）。
  2. 股票池（與模型追蹤相同：每季依成交金額排名的大型、中型、上櫃，另加觀察清單）中未持有、燈號「上車」的股票，
     依分數（法人 5 日累計買超 ÷ 成交量）由高到低，填滿空出的份數 → 隔天開盤買進，每份＝當時總資產 ÷ 份數。
- 隔天開盤成交：賣出先、買進後；停牌沒有開盤價的，賣單留到下一個有成交的日子，買單取消。
- 計價用還原股價（除權息不會讓資產憑空減少）；手續費、證交稅依設定扣除。
- 對照：同一天開始持有 0050（還原股息）。
- 只從「上線後」的日子往後記錄：補抓歷史資料時不記（見 jobs.Crawler.track）。
"""
from __future__ import annotations

from .analysis import DEFAULT_RULES, evaluate
from .backtest import BTConfig, History, rank_universe
from .db import DB
from .tracking import WARMUP, _back, _quarter_start, _trading, rules_version

BOOKS = (10, 20, 30)
BENCH = "0050"


def _score(rec: dict) -> float | None:
    f5, t5, v5 = rec.get("foreign5d"), rec.get("trust5d"), rec.get("vol5")
    if not v5 or (f5 is None and t5 is None):
        return None
    return ((f5 or 0) + (t5 or 0)) / (v5 * 5) * 100


def _universe(db: DB, S: dict, trading: list[str], d: str) -> dict[str, set[str]]:
    u = {c: set(t) for c, t in rank_universe(db, _quarter_start(trading, d), BTConfig("", "", "")).items()}
    for w in S.get("watchlist") or []:
        u.setdefault(w["code"], set()).add("觀察清單")
    return u


def _last(hist: History, code: str, d: str, key: str = "close") -> float | None:
    """code 在 d（含）以前最近一個有成交日的還原價。"""
    a = hist.adj.get(code) or {}
    b = a.get(d)
    if b and b.get(key):
        return b[key]
    for x in reversed(hist.qd.get(code) or []):
        if x <= d and a.get(x) and a[x].get("close"):
            return a[x]["close"]
    return None


def _units(hist: History, pos, fee: float) -> float | None:
    """持股（以還原價計的單位數）。每天用當天載入的還原價重算：兩個日子之間的還原比例不會因為之後的除權息而改變。"""
    b = (hist.adj.get(pos["code"]) or {}).get(pos["entry_date"])
    px = b and (b.get("open") or b.get("close"))
    return pos["alloc"] / (px * (1 + fee)) if px else None


def started(db: DB) -> bool:
    return bool(db.one("SELECT 1 AS x FROM paper_nav LIMIT 1"))


def step(db: DB, S: dict, d: str, books=BOOKS) -> int:
    """處理到 d（含）為止還沒處理的交易日；第一次執行時從 d 開始。回傳處理的天數。已處理過的日子不會重算。"""
    trading = _trading(db)
    if d not in trading:
        return 0
    last = {r["book"]: r["d"] for r in db.q("SELECT book, MAX(date) AS d FROM paper_nav GROUP BY book")}
    todo = sorted({x for b in books for x in ([y for y in trading if last[b] < y <= d] if b in last else [d])})
    for x in todo:
        _day(db, S, trading, x, [b for b in books if last.get(b, "") < x])
    return len(todo)


def _day(db: DB, S: dict, trading: list[str], d: str, books: list[int]) -> None:
    P = {**DEFAULT_RULES, **{p["id"]: p["value"] for p in S["params"]}}
    fee, tax = P.get("feePct", 0.1425) / 100, P.get("taxPct", 0.3) / 100
    ver = rules_version(S)
    uni = _universe(db, S, trading, d)
    pos_rows = [dict(r) for r in db.q("SELECT * FROM paper_pos")]
    ord_rows = [dict(r) for r in db.q("SELECT * FROM paper_order")]
    since = min([_back(trading, d, WARMUP), *(r["entry_date"] for r in pos_rows)])
    i = trading.index(d)
    prev = trading[i - 1] if i else None
    codes = sorted(set(uni) | {r["code"] for r in pos_rows} | {r["code"] for r in ord_rows} | {BENCH})
    hist = History(db, codes, d, since=min(since, prev or d))
    recs: dict[str, dict] = {}

    def rec(code):
        if code not in recs:
            r = hist.rec(code, d, P) if (hist.raw.get(code) or {}).get(d) else None
            recs[code] = r if r and r.get("close") is not None else None
        return recs[code]
    entry_sig: dict[str, tuple] = {}

    def entry(code):   # 未持有時的判定（三本帳共用）
        if code not in entry_sig:
            r = rec(code)
            sig = evaluate(r, {"code": code}, P, S) if r else None
            entry_sig[code] = (sig, _score(r) if r else None)
        return entry_sig[code]

    names = {r["code"]: r["name"] for r in db.q(f"SELECT code, name FROM quotes WHERE date=? AND code IN ({','.join('?' * len(codes))})",
                                                (d, *codes))}
    for cd in uni:      # 先算好（不佔資料庫鎖）
        entry(cd)
    nav_rows, logs = [], []
    with db.tx() as c:
        for book in books:
            pos = {r["code"]: r for r in pos_rows if r["book"] == book}
            orders = [r for r in ord_rows if r["book"] == book and r["sig_date"] < d]
            prev_nav = db.one("SELECT * FROM paper_nav WHERE book=? ORDER BY date DESC LIMIT 1", (book,))
            cash = prev_nav["cash"] if prev_nav else 1.0
            # 1. 開盤成交：賣出先
            for o in sorted(orders, key=lambda o: (o["side"] != "sell", -(o["score"] if o["score"] is not None else -1e9), o["code"])):
                raw = (hist.raw.get(o["code"]) or {}).get(d)
                bar = (hist.adj.get(o["code"]) or {}).get(d)
                ok = raw and bar and raw.get("close")
                if o["side"] == "sell":
                    p = pos.get(o["code"])
                    if p is None:
                        c.execute("DELETE FROM paper_order WHERE book=? AND code=?", (book, o["code"]))
                        continue
                    if not ok:
                        continue          # 停牌：留到下一個有成交的日子
                    u = _units(hist, p, fee)
                    got = u * (bar["open"] or bar["close"]) * (1 - fee - tax) if u else 0.0
                    cash += got
                    logs.append((book, d, o["code"], "賣出", raw["open"] or raw["close"], got, round((got / p["alloc"] - 1) * 100, 2),
                                 o["rule"], o["why"], o["sig_date"], ver))
                    c.execute("DELETE FROM paper_pos WHERE book=? AND code=?", (book, o["code"]))
                    del pos[o["code"]]
                else:
                    if not ok or len(pos) >= book or o["code"] in pos:
                        logs.append((book, d, o["code"], "未成交", None, 0.0, None, o["rule"],
                                     "停牌或無開盤價" if not ok else "沒有空的份數", o["sig_date"], ver))
                    else:
                        eq = cash + sum((_units(hist, p, fee) or 0) * (_last(hist, cd, prev or d) or 0) for cd, p in pos.items())
                        alloc = min(cash, eq / book)
                        if alloc > 0:
                            cash -= alloc
                            p = {"book": book, "code": o["code"], "entry_date": d, "entry_px": raw["open"] or raw["close"],
                                 "alloc": alloc, "score": o["score"], "rule": o["rule"], "why": o["why"], "sig_date": o["sig_date"]}
                            c.execute("INSERT INTO paper_pos(book,code,entry_date,entry_px,alloc,score,rule,why,sig_date) "
                                      "VALUES(:book,:code,:entry_date,:entry_px,:alloc,:score,:rule,:why,:sig_date)", p)
                            pos[o["code"]] = p
                            logs.append((book, d, o["code"], "買進", p["entry_px"], alloc, None, o["rule"], o["why"], o["sig_date"], ver))
                c.execute("DELETE FROM paper_order WHERE book=? AND code=? AND side=?", (book, o["code"], o["side"]))
            # 2. 收盤判定：持有中的股票
            sells = 0
            pending = {r["code"] for r in db.q("SELECT code FROM paper_order WHERE book=?", (book,))}
            for cd, p in pos.items():
                r = rec(cd)
                if r is None or cd in pending:   # 今天沒成交，或賣單還沒成交（停牌）
                    continue
                f = hist.factor(cd, d) or 1.0
                u = _units(hist, p, fee)
                cost = p["alloc"] / u if u else None   # 還原價成本（含手續費）
                a = hist.adj[cd]
                peak = max((a[x]["close"] for x in hist.qd[cd] if p["entry_date"] <= x <= d and a.get(x) and a[x].get("close")),
                           default=r["close"])
                stock = {"code": cd, "holding": True, "cost": cost / f if cost else None, "buyDate": p["entry_date"]}
                sig = evaluate(dict(r, peakClose=peak / f), stock, P, S)
                h = sig.get("hold") or {}
                if h.get("light") == "exit":
                    c.execute("INSERT OR REPLACE INTO paper_order(book,code,side,sig_date,rule,why,score) VALUES(?,?,?,?,?,?,?)",
                              (book, cd, "sell", d, h.get("rule") or "", "、".join((h.get("reasons") or [])[:3]), None))
                    sells += 1
            # 3. 收盤判定：上車候選，依分數填滿空出的份數
            free = book - len(pos) + sells
            cands = []
            for cd in uni:
                if cd in pos:
                    continue
                sig, sc = entry(cd)
                if sig and sig["light"] == "go" and sig.get("rule") != "加碼":
                    cands.append((-(sc if sc is not None else -1e9), cd, sig, sc))
            for _, cd, sig, sc in sorted(cands)[:max(0, free)]:
                c.execute("INSERT OR REPLACE INTO paper_order(book,code,side,sig_date,rule,why,score) VALUES(?,?,?,?,?,?,?)",
                          (book, cd, "buy", d, sig.get("rule") or "", "、".join((sig.get("entryReasons") or [])[:3]), sc))
            # 4. 收盤計價
            held = sum((_units(hist, p, fee) or 0) * (_last(hist, cd, d) or 0) for cd, p in pos.items())
            b0, b1 = _last(hist, BENCH, prev) if prev else None, _last(hist, BENCH, d)
            if prev_nav is None:
                bench = 1.0 if b1 else None
            elif prev_nav["bench"] is not None and b0 and b1:
                bench = prev_nav["bench"] * b1 / b0
            else:
                bench = prev_nav["bench"]
            nav_rows.append((book, d, cash + held, cash, len(pos), bench, ver, len(cands)))
        c.executemany("INSERT OR REPLACE INTO paper_nav(book,date,equity,cash,npos,bench,version,cands) VALUES(?,?,?,?,?,?,?,?)", nav_rows)
        c.executemany("INSERT INTO paper_log(book,date,code,side,px,amount,ret,rule,why,sig_date,version) VALUES(?,?,?,?,?,?,?,?,?,?,?)", logs)
    if names:
        db.set_setting("paperNames", {**(db.get_setting("paperNames") or {}), **{k: v for k, v in names.items() if v}})


def _dd(xs: list[float]) -> float | None:
    peak, dd = None, 0.0
    for v in xs:
        if v is None:
            continue
        peak = v if peak is None else max(peak, v)
        dd = min(dd, v / peak - 1)
    return round(dd * 100, 2) if peak else None


def summary(db: DB, books=BOOKS, log_n: int = 40) -> dict:
    names = db.get_setting("paperNames") or {}
    out = {"books": [], "started": started(db)}
    for book in books:
        nav = [dict(r) for r in db.q("SELECT * FROM paper_nav WHERE book=? ORDER BY date", (book,))]
        if not nav:
            continue
        last = nav[-1]["date"]
        px = {r["code"]: r["close"] for r in db.q("SELECT code, close FROM quotes WHERE date=?", (last,))}
        pos = []
        for r in db.q("SELECT * FROM paper_pos WHERE book=? ORDER BY entry_date, code", (book,)):
            now = px.get(r["code"])
            pos.append({"code": r["code"], "name": names.get(r["code"], ""), "entry": r["entry_date"], "entryPx": r["entry_px"],
                        "close": now, "retPct": round((now / r["entry_px"] - 1) * 100, 2) if now and r["entry_px"] else None,
                        "rule": r["rule"]})
        eq = nav[-1]["equity"]
        logs = [dict(r, name=names.get(r["code"], "")) for r in
                db.q("SELECT * FROM paper_log WHERE book=? ORDER BY date DESC, id DESC LIMIT ?", (book, log_n))]
        sold = [r["ret"] for r in db.q("SELECT ret FROM paper_log WHERE book=? AND side='賣出'", (book,)) if r["ret"] is not None]
        orders = [dict(r, name=names.get(r["code"], "")) for r in db.q("SELECT * FROM paper_order WHERE book=? ORDER BY side DESC, score DESC", (book,))]
        out["books"].append({
            "slots": book, "start": nav[0]["date"], "latest": last, "days": len(nav),
            "totalPct": round((eq - 1) * 100, 2),
            "benchPct": round((nav[-1]["bench"] - 1) * 100, 2) if nav[-1]["bench"] is not None else None,
            "maxDD": _dd([r["equity"] for r in nav]), "benchMaxDD": _dd([r["bench"] for r in nav]),
            "cashPct": round(nav[-1]["cash"] / eq * 100, 1) if eq else None, "npos": nav[-1]["npos"],
            "closed": len(sold), "winPct": round(sum(x > 0 for x in sold) / len(sold) * 100, 1) if sold else None,
            "avgRet": round(sum(sold) / len(sold), 2) if sold else None,
            "versions": sorted({r["version"] for r in nav}),
            "curve": [[r["date"], round(r["equity"], 5), round(r["bench"], 5) if r["bench"] is not None else None] for r in nav],
            "positions": pos, "orders": orders, "log": logs})
    return out


def reset(db: DB) -> None:
    with db.tx() as c:
        for t in ("paper_nav", "paper_pos", "paper_order", "paper_log"):
            c.execute(f"DELETE FROM {t}")
