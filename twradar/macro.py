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


FWD = (10, 20)   # 歷史統計：之後 10、20 個交易日的加權指數報酬

CONCEPT = {
    ("foreign", "sell_add"): "現貨賣、期貨也加空，是外資最一致的看空：不只賣掉持股，還押注大盤下跌。",
    ("foreign", "sell_cover"): "外資常先用期貨避險再賣現貨；空單開始回補，代表押注下跌的部位在減少，賣壓常會跟著減輕。但現貨還在賣，表示還沒轉向。",
    ("foreign", "buy_cover"): "現貨買、空單也回補，外資兩邊都轉向偏多，常是波段回升的開始。",
    ("foreign", "buy_add"): "現貨買、期貨卻加空，常是一邊買股票一邊用期貨避險，或只是換股，方向不明確。",
    ("foreign", "sell_flat"): "外資只在現貨賣、期貨沒有明顯動作，比較像調節持股，不一定是看空整個市場。",
    ("foreign", "buy_flat"): "外資現貨買超、期貨沒有明顯動作，偏多但力道普通。",
    ("futlevel", "high"): "淨空單越多，代表押注下跌的部位越大；但空單也是未來的買盤：回補時必須買回期貨，常推升大盤（軋空）。",
    ("fx", "depr"): "外資賣股後把新台幣換成美元匯出，新台幣就會貶值；匯率是確認錢是否真的離開台灣的指標。",
    ("fx", "near_depr"): "新台幣小幅走貶，資金有外流的跡象，但還不明顯。",
    ("fx", "appr"): "新台幣升值，常代表外資把錢匯進來，資金面偏多。",
    ("fx", "near_appr"): "新台幣小幅走升，資金可能開始回流。",
    ("fx", "flat"): "匯率持平，代表外資賣股的錢可能還留在台灣（放現金或轉買其他資產），還沒真的匯出。",
    ("breadth", "b0"): "幾乎所有股票都跌破月線，是恐慌性的全面賣出；這種極端常出現在下跌的後段，但也可能再持續一段時間。",
    ("breadth", "b10"): "大多數股票走弱，市場偏空，個股訊號容易失敗。",
    ("breadth", "b30"): "不到一半的股票在月線上，行情集中在少數族群，選股難度高。",
    ("breadth", "b50"): "多數股票在月線上，市場普遍健康。",
    ("breadth", "b80"): "幾乎全面上漲，市場情緒熱，常伴隨短線過熱與拉回。",
    ("diverge", "yes"): "指數漲、多數股票卻跌，代表只有少數權值股在撐，行情基礎不穩。",
    ("state", "exit"): "這是系統規則，不是預測：外資在現貨、期貨、匯率同時撤出時暫停上車（回溯顯示能減少虧損）。",
    ("state", "ok"): "這是系統規則，不是預測：外資沒有同時在三個地方撤出，個股訊號照常判斷。",
}


def classify(f: dict, P: dict, fut_hi: float | None = None) -> dict:
    """把一天的大盤環境分類成幾種情境（每一組最多一種）。"""
    out = {}
    sp, fc, fx = f.get("foreignSpot5d"), f.get("futForeignChg5"), f.get("usdTwdChg5Pct")
    lots, thr = P.get("foreignFutChgLots", 2000), P.get("foreignFxPct", 0.3)
    if sp is not None and fc is not None:
        fut = "add" if fc <= -lots else "cover" if fc >= lots else "flat"
        out["foreign"] = ("sell_" if sp < 0 else "buy_") + fut
    if fut_hi is not None and f.get("futForeign") is not None and f["futForeign"] <= fut_hi < 0:
        out["futlevel"] = "high"
    if fx is not None:
        out["fx"] = ("depr" if fx >= thr else "appr" if fx <= -thr else "near_depr" if fx >= thr * .6
                     else "near_appr" if fx <= -thr * .6 else "flat")
    b = f.get("breadth20")
    if b is not None:
        out["breadth"] = "b0" if b < 10 else "b10" if b < 30 else "b30" if b < 50 else "b50" if b < 80 else "b80"
    mc, bc = f.get("mktChg10Pct"), f.get("breadth20Chg10")
    if mc is not None and bc is not None and mc > 0 and bc <= -P.get("breadthDropPts", 10):
        out["diverge"] = "yes"
    hit, have, _ = foreign_signals(f, P)
    need = P.get("foreignExitSignals", 3)
    if have >= need:
        out["state"] = "exit" if hit >= need else "ok"
    return out


def _fut_hi(db: DB, d: str) -> float | None:
    """淨空單「偏高」的門檻：到 d 為止，空單最多的 15% 日子的界線。"""
    xs = [r["fut_foreign"] for r in db.q("SELECT fut_foreign FROM macro WHERE fut_foreign IS NOT NULL AND date<=? ORDER BY fut_foreign", (d,))]
    return xs[int(len(xs) * .15)] if len(xs) >= 60 else None


def situation_stats(db: DB, d: str, P: dict) -> dict:
    """過去每一種情境出現後，加權指數之後 10、20 個交易日的表現（只用 d 以前、之後走勢已完整的日子）。"""
    M = Macro(db, d)
    fut_hi = _fut_hi(db, d)
    acc: dict = {}
    base = {h: [] for h in FWD}
    n = len(M.days)
    for i in range(n - max(FWD)):
        if not M.taiex[i]:
            continue
        rets = {h: M.taiex[i + h] / M.taiex[i] - 1 for h in FWD}
        for h in FWD:
            base[h].append(rets[h])
        for g, k in classify(M.fields(M.days[i]), P, fut_hi).items():
            acc.setdefault((g, k), []).append((i, rets))

    def summ(rows):
        if not rows:
            return {"n": 0}
        eps = 1 + sum(1 for a, b in zip(rows, rows[1:]) if b[0] - a[0] > 10)
        out = {"n": len(rows), "episodes": eps}
        for h in FWD:
            xs = [r[1][h] for r in rows]
            out[f"f{h}"] = round(sum(xs) / len(xs) * 100, 2)
            out[f"up{h}"] = round(sum(x > 0 for x in xs) / len(xs) * 100)
        return out

    bs = {"n": len(base[FWD[0]])}
    for h in FWD:
        xs = base[h]
        if xs:
            bs[f"f{h}"] = round(sum(xs) / len(xs) * 100, 2)
            bs[f"up{h}"] = round(sum(x > 0 for x in xs) / len(xs) * 100)
    return {"since": M.days[0] if M.days else None, "base": bs, "futHi": fut_hi,
            "by": {f"{g}:{k}": summ(v) for (g, k), v in acc.items()}}


def evidence(st: dict | None, key: str) -> str:
    if not st:
        return ""
    s, b = st["by"].get(key, {"n": 0}), st["base"]
    since = st.get("since") or ""
    head = f"過去資料（{since[:4]}/{since[4:6]} 起）"
    if s["n"] < 5 or s.get("episodes", 0) < 2:
        return f"{head}：這種情況只出現過 {s['n']} 天，樣本太少，無法判斷。"
    h = max(FWD)
    return (f"{head}：出現過 {s['n']} 天（約 {s['episodes']} 段）；之後 {h} 個交易日加權指數平均 {s[f'f{h}']:+.1f}%、"
            f"上漲機率 {s[f'up{h}']}%（所有日子平均 {b.get(f'f{h}', 0):+.1f}%、上漲 {b.get(f'up{h}', 0)}%）。")


def reading(f: dict, rank: dict, state: str, hit: int, need: int, P: dict, stats: dict | None = None) -> list[dict]:
    """大盤環境的情境解讀：每一項有三段：現況（數字）、一般解讀（常識）、過去資料（我們自己的歷史統計）。"""
    out: list[dict] = []
    cls = classify(f, P, (stats or {}).get("futHi"))
    item = lambda g, text: out.append({"text": text, "concept": CONCEPT.get((g, cls.get(g)), ""),  # noqa: E731
                                       "evidence": evidence(stats, f"{g}:{cls.get(g)}")})
    sp, fc, fx = f.get("foreignSpot5d"), f.get("futForeignChg5"), f.get("usdTwdChg5Pct")
    if "foreign" in cls:
        k = cls["foreign"]
        fut = {"add": f"期貨淨空單 5 日增加 {-fc:,} 口", "cover": f"期貨淨空單 5 日減少 {fc:,} 口（回補）",
               "flat": f"期貨淨空單 5 日變化不大（{fc:+,} 口）"}[k.split("_")[1]]
        item("foreign", f"外資現貨 5 日{'賣超' if sp < 0 else '買超'} {abs(sp):,.0f} 億，{fut}。")
    elif sp is not None:
        out.append({"text": f"外資現貨 5 日{'賣超' if sp < 0 else '買超'} {abs(sp):,.0f} 億（期貨資料不足）。", "concept": "", "evidence": ""})
    fr = rank.get("futForeign")
    if "futlevel" in cls and fr:
        item("futlevel", f"外資台指期淨空單 {-f['futForeign']:,} 口，比近 {fr['days']} 個交易日中 {100 - fr['pct']:.0f}% 的日子都多。")
    if "fx" in cls:
        thr = P.get("foreignFxPct", 0.3)
        v = f"{abs(fx):.3f}".rstrip("0") if round(abs(fx), 2) == thr and abs(fx) != thr else f"{abs(fx):.2f}"   # 0.298 不要顯示成 0.30
        word = "貶" if fx > 0 else "升"
        tail = {"depr": "", "appr": "", "near_depr": f"，接近 {thr:g}% 的門檻", "near_appr": f"，接近 {thr:g}% 的門檻", "flat": "，大致持平"}[cls["fx"]]
        item("fx", f"新台幣 5 日{word} {v}%{tail}。")
    b = f.get("breadth20")
    if "breadth" in cls:
        br = rank.get("breadth20")
        pos = f"（近 {br['days']} 個交易日中，只有 {br['pct']:.0f}% 的日子這麼低或更低）" if br and br["pct"] <= 20 else ""
        item("breadth", f"市場廣度 {b:.0f}%：{'幾乎所有' if b < 10 else '大多數' if b < 30 else '超過一半的' if b < 50 else '只有少數' if b < 80 else '幾乎沒有'}股票在月線下{pos}。")
    if "diverge" in cls:
        item("diverge", f"加權指數 10 日漲 {f['mktChg10Pct']:.1f}%，但廣度降了 {-f['breadth20Chg10']:.0f} 點。")
    if state == "exit":
        item("state", f"外資撤出成立（{hit}/{need} 項）：系統暫停新的上車訊號，持股照原本的出場規則。")
    elif state == "ok":
        near = "，已接近暫停門檻" if hit == need - 1 and hit > 0 else ""
        item("state", f"外資撤出未成立（{hit}/{need} 項{near}）：系統照常判斷個股訊號。")
    return out
