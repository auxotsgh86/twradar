"""大盤環境：外資態度（現貨、台指期淨未平倉、新台幣匯率）與市場廣度。

資料：market（加權指數、外資現貨買賣超金額）、macro（期交所外資期貨淨口數、美元兌新台幣、市場廣度）。
所有欄位都只用判定日當天（含）以前的資料：期貨與匯率在收盤後公布，判定在收盤後、隔日開盤才執行。
"""
from __future__ import annotations

import bisect
from collections import deque

from .db import DB

WIN = 5          # 外資態度看近 5 個交易日
BREADTH_N = 20   # 廣度：站上 20 日均線的比例
LOOKBACK = 10    # 廣度與指數的比較基準：10 個交易日前


def _common(code: str) -> bool:
    """普通股：4 碼數字、不是 0 開頭（排除 ETF、權證等）。"""
    return len(code) == 4 and code.isdigit() and code[0] != "0"


def compute_breadth(db: DB, dates: list[str] | None = None) -> int:
    """計算並存入 dates（預設：全部交易日中尚未計算的）每天的市場廣度：普通股收盤站上自己 20 日均線的比例（%）。"""
    trading = [r["date"] for r in db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]
    if not trading:
        return 0
    if dates is None:
        have = {r["date"] for r in db.q("SELECT date FROM macro WHERE breadth20 IS NOT NULL")}
        dates = [d for d in trading[BREADTH_N - 1:] if d not in have]
    want = sorted(set(dates) & set(trading))
    if not want:
        return 0
    ix = {d: i for i, d in enumerate(trading)}
    start = trading[max(0, ix[want[0]] - BREADTH_N + 1)]
    win: dict[str, deque] = {}
    sums: dict[str, float] = {}
    rows: dict[str, list] = {}
    for r in db.q("SELECT date, code, close FROM quotes WHERE date>=? AND date<=? AND close IS NOT NULL ORDER BY date",
                  (start, want[-1])):
        if _common(r["code"]):
            rows.setdefault(r["date"], []).append((r["code"], r["close"]))
    wset, out = set(want), []
    for d in trading[ix[start]:ix[want[-1]] + 1]:
        i = ix[d]
        up = n = 0
        for code, close in rows.get(d, []):
            q = win.get(code)
            if q is None:
                q = win[code] = deque()
                sums[code] = 0.0
            q.append((i, close))
            sums[code] += close
            while q and q[0][0] <= i - BREADTH_N:
                sums[code] -= q.popleft()[1]
            if len(q) == BREADTH_N:
                n += 1
                up += close > sums[code] / BREADTH_N
        if d in wset and n >= 50:
            out.append({"date": d, "breadth20": round(up / n * 100, 2)})
    db.put_macro(out)
    return len(out)


class Macro:
    """一次載入 since～until 的大盤環境資料；fields(d) 回傳判定日 d 的欄位。"""

    def __init__(self, db: DB, until: str, since: str = ""):
        rng = (since, until)
        m = db.q("SELECT date, taiex, foreign_amt FROM market WHERE taiex IS NOT NULL AND date>=? AND date<=? ORDER BY date", rng)
        self.days = [r["date"] for r in m]
        self.taiex = [r["taiex"] for r in m]
        self.spot = [r["foreign_amt"] for r in m]
        mac = {r["date"]: dict(r) for r in db.q("SELECT * FROM macro WHERE date>=? AND date<=?", rng)}
        self.fut = [mac.get(d, {}).get("fut_foreign") for d in self.days]
        self.fx = [mac.get(d, {}).get("usd_twd") for d in self.days]
        self.br = [mac.get(d, {}).get("breadth20") for d in self.days]
        self._cache: dict[str, dict] = {}

    def fields(self, d: str) -> dict:
        if d in self._cache:
            return self._cache[d]
        i = bisect.bisect_right(self.days, d) - 1
        out: dict = {}
        if i >= 0:
            j = i - WIN
            sp = self.spot[i - WIN + 1:i + 1] if i - WIN + 1 >= 0 else []
            if len(sp) == WIN and None not in sp:
                out["foreignSpot5d"] = round(sum(sp) / 1e8, 1)           # 億元
            if self.fut[i] is not None:
                out["futForeign"] = self.fut[i]
                if j >= 0 and self.fut[j] is not None:
                    out["futForeignChg5"] = self.fut[i] - self.fut[j]     # 口；負＝空單增加
            if self.fx[i] is not None:
                out["usdTwd"] = self.fx[i]
                if j >= 0 and self.fx[j]:
                    out["usdTwdChg5Pct"] = round((self.fx[i] / self.fx[j] - 1) * 100, 3)   # 正＝新台幣貶值
            k = i - LOOKBACK
            if self.br[i] is not None:
                out["breadth20"] = self.br[i]
                if k >= 0 and self.br[k] is not None:
                    out["breadth20Chg10"] = round(self.br[i] - self.br[k], 2)
            if k >= 0 and self.taiex[k]:
                out["mktChg10Pct"] = round((self.taiex[i] / self.taiex[k] - 1) * 100, 2)
        self._cache[d] = out
        return out


def foreign_signals(rec: dict, P: dict) -> tuple[int, int, list[str]]:
    """外資撤出的訊號數：（成立數, 有資料的數, 說明）。"""
    hit, have, why = 0, 0, []
    sp, fc, fx = rec.get("foreignSpot5d"), rec.get("futForeignChg5"), rec.get("usdTwdChg5Pct")
    if sp is not None:
        have += 1
        if sp < 0:
            hit += 1
            why.append(f"現貨 5 日賣超 {-sp:,.0f} 億")
    if fc is not None:
        have += 1
        if fc <= -P["foreignFutChgLots"]:
            hit += 1
            why.append(f"期貨淨空單 5 日增加 {-fc:,} 口")
    if fx is not None:
        have += 1
        if fx >= P["foreignFxPct"]:
            hit += 1
            why.append(f"新台幣 5 日貶 {fx:.2f}%")
    return hit, have, why


def history_rank(db: DB, d: str, rec: dict) -> dict:
    """目前數值在歷史（macro 表全部日子）中的位置：低於或等於它的天數比例（%）。"""
    out = {}
    for key, col in (("breadth20", "breadth20"), ("futForeign", "fut_foreign")):
        v = rec.get(key)
        if v is None:
            continue
        r = db.one(f"SELECT COUNT(*) AS n, SUM({col} <= ?) AS le, MIN(date) AS a FROM macro WHERE {col} IS NOT NULL AND date<=?", (v, d))
        if r and r["n"] >= 60:
            out[key] = {"pct": round(r["le"] / r["n"] * 100, 1), "days": r["n"], "since": r["a"]}
    return out


def reading(f: dict, rank: dict, state: str, hit: int, need: int, P: dict) -> list[str]:
    """把大盤環境的數字翻成白話的情境解讀（規則式，非預測）。"""
    out: list[str] = []
    sp, fc, fx = f.get("foreignSpot5d"), f.get("futForeignChg5"), f.get("usdTwdChg5Pct")
    lots = P.get("foreignFutChgLots", 2000)
    adding = fc is not None and fc <= -lots
    covering = fc is not None and fc >= lots
    if sp is not None:
        sell = sp < 0
        fut = ("期貨空單還在增加" if adding else f"期貨空單 5 日回補 {fc:,} 口" if covering
               else "期貨空單變化不大" if fc is not None else "")
        if sell and adding:
            s = f"外資現貨 5 日賣超 {-sp:,.0f} 億，{fut}：外資在現貨和期貨兩邊同時看空。"
        elif sell and covering:
            s = f"外資現貨 5 日還在賣（{-sp:,.0f} 億），但{fut}：賣壓可能接近尾聲，但還不是回頭買進。"
        elif not sell and covering:
            s = f"外資現貨 5 日買超 {sp:,.0f} 億，{fut}：外資回頭的訊號。"
        elif not sell and adding:
            s = f"外資現貨 5 日買超 {sp:,.0f} 億，但{fut}：可能是換股或避險，方向不明。"
        else:
            s = f"外資現貨 5 日{'賣超' if sell else '買超'} {abs(sp):,.0f} 億" + (f"，{fut}。" if fut else "。")
        out.append(s)
    fr = rank.get("futForeign")
    if fr and f.get("futForeign", 0) < 0 and fr["pct"] <= 15:
        out.append(f"外資台指期淨空單 {-f['futForeign']:,} 口，比近 {fr['days']} 個交易日中 {100 - fr['pct']:.0f}% 的日子都多，屬於偏高的空單部位；"
                   "空單大量回補通常是止跌的重要訊號。")
    if fx is not None:
        if fx >= P.get("foreignFxPct", 0.3):
            out.append(f"新台幣 5 日貶 {fx:.2f}%：資金外流的壓力還在。")
        elif fx <= -P.get("foreignFxPct", 0.3):
            out.append(f"新台幣 5 日升 {-fx:.2f}%：資金有回流跡象。")
        else:
            thr = P.get("foreignFxPct", 0.3)
            v = f"{abs(fx):.3f}".rstrip("0") if round(abs(fx), 2) >= thr else f"{abs(fx):.2f}"
            if fx >= thr * .6:
                out.append(f"新台幣 5 日小貶 {v}%，接近 {thr:g}% 的門檻，留意資金外流。")
            elif fx <= -thr * .6:
                out.append(f"新台幣 5 日小升 {v}%，接近 {thr:g}% 的門檻，資金可能開始回流。")
            else:
                out.append(f"新台幣 5 日變動 {fx:+.2f}%，大致持平，匯率沒有明顯的資金進出訊號。")
    b = f.get("breadth20")
    if b is not None:
        br = rank.get("breadth20")
        pos = f"（近 {br['days']} 個交易日中，只有 {br['pct']:.0f}% 的日子這麼低或更低）" if br and br["pct"] <= 20 else ""
        if b < 10:
            out.append(f"市場廣度 {b:.0f}%：幾乎所有股票都在月線下{pos}，屬於極端弱勢；這種位置常是賣壓宣洩的後段，"
                       "但還無法確定是底部，最好等外資空單回補或廣度回升再確認。")
        elif b < 30:
            out.append(f"市場廣度 {b:.0f}%：大多數股票在月線下{pos}，市場偏弱。")
        elif b < 50:
            out.append(f"市場廣度 {b:.0f}%：月線上的股票不到一半，強勢股集中在少數族群。")
        elif b < 80:
            out.append(f"市場廣度 {b:.0f}%：多數股票在月線上，市場普遍偏多。")
        else:
            out.append(f"市場廣度 {b:.0f}%：幾乎全面上漲，短線偏熱，追高要小心。")
        mc, bc = f.get("mktChg10Pct"), f.get("breadth20Chg10")
        if mc is not None and bc is not None and mc > 0 and bc <= -10:
            out.append(f"加權指數 10 日漲 {mc:.1f}%，但廣度降了 {-bc:.0f} 點：只有少數權值股在撐盤。")
    if state == "exit":
        out.append(f"外資撤出成立（{hit}/{need} 項）：系統暫停新的上車訊號，持股照原本的出場規則。")
    elif state == "ok":
        near = "，已接近暫停門檻" if hit == need - 1 and hit > 0 else ""
        out.append(f"外資撤出未成立（{hit}/{need} 項{near}）：系統照常判斷個股訊號。")
    return out
