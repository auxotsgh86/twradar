"""完整測試：解析器（用實際格式）、訊號判定、端到端（假交易所伺服器 → 抓取 → 分析 → API）。

執行：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["TWRADAR_HOME"] = TMP

from twradar import sources as S  # noqa: E402
from twradar.analysis import DEFAULT_RULES, evaluate  # noqa: E402
from twradar.config import load_config  # noqa: E402
from twradar.net import Fetcher  # noqa: E402

# ---------------- 依 2026/11 實際回應整理的格式 ----------------
T86_FIELDS = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
              "外陸資買賣超股數(不含外資自營商)", "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
              "投信買進股數", "投信賣出股數", "投信買賣超股數", "自營商買賣超股數", "自營商買進股數(自行買賣)",
              "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)", "自營商賣出股數(避險)",
              "自營商買賣超股數(避險)", "三大法人買賣超股數"]
MARGN_FIELDS = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額", "買進", "賣出",
                "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"]
QUOTE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
                "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"]
IDX_FIELDS = ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"]


def c(n, d=0):
    return f"{n:,.{d}f}"


class FakeExchange:
    """產生 N 個交易日的假資料，格式比照證交所／櫃買中心。"""

    def __init__(self, days, holiday=None, no_margin=None, block_tpex=False, otc_ok=True):
        self.days = days
        self.holiday = holiday or set()
        self.no_margin = no_margin or set()
        self.block_tpex = block_tpex
        self.otc_ok = otc_ok
        self.stocks = {"2330": ("台積電", "TWSE", 2000.0), "2454": ("聯發科", "TWSE", 3500.0), "6488": ("環球晶", "TPEx", 600.0)}
        self.px = {}
        for code, (_, _, base) in self.stocks.items():
            p = base
            for i, d in enumerate(days):
                prev = p
                p = round(base * (1 + 0.002 * i) + (5 if i % 2 else -3), 1)
                self.px[(code, d)] = (prev, p, 10_000_000 + i * 100_000)

    def handle(self, path, qs):
        if "finmind" in path:
            ds = qs["dataset"][0]
            sid = qs["data_id"][0]
            st, en = S.norm_date(qs["start_date"][0]), S.norm_date(qs["end_date"][0])
            data = []
            for dd in self.days:
                if not (st <= dd <= en) or (sid, dd) not in self.px:
                    continue
                prev, p, v = self.px[(sid, dd)]
                iso = f"{dd[:4]}-{dd[4:6]}-{dd[6:]}"
                if ds == "TaiwanStockPrice":
                    data.append({"date": iso, "stock_id": sid, "Trading_Volume": v, "Trading_money": v * p, "open": prev,
                                 "max": p + 5, "min": p - 5, "close": p, "spread": round(p - prev, 2), "Trading_turnover": 1})
                elif ds == "TaiwanStockInstitutionalInvestorsBuySell":
                    data += [{"date": iso, "stock_id": sid, "buy": 400_000, "sell": 100_000, "name": "Foreign_Investor"},
                             {"date": iso, "stock_id": sid, "buy": 0, "sell": 100_000, "name": "Investment_Trust"},
                             {"date": iso, "stock_id": sid, "buy": 10_000, "sell": 0, "name": "Dealer_self"}]
                else:
                    data.append({"date": iso, "stock_id": sid, "MarginPurchaseYesterdayBalance": 9612,
                                 "MarginPurchaseTodayBalance": 9653, "ShortSaleYesterdayBalance": 202, "ShortSaleTodayBalance": 195})
            return 200, json.dumps({"msg": "success", "status": 200, "data": data}).encode()
        d = (qs.get("date") or qs.get("dayDate") or qs.get("d") or [""])[0]
        d = S.norm_date(d) if d else ""
        nodata = {"stat": "很抱歉，沒有符合條件的資料!"}
        if "/tpex/" in path and self.block_tpex:
            return 403, b"<html>FOR SECURITY REASONS</html>"
        if d in self.holiday or (d and d not in self.days):
            return 200, json.dumps(nodata if "/twse/" in path else {"tables": [], "date": d}).encode()
        i = self.days.index(d) if d else 0
        if path.endswith("/MI_INDEX"):
            rows = []
            for code, (name, mkt, _) in self.stocks.items():
                if mkt != "TWSE":
                    continue
                prev, p, v = self.px[(code, d)]
                sign = '<p style= color:red>+</p>' if p >= prev else '<p style= color:green>-</p>'
                rows.append([code, name, c(v), "1,000", c(v * p), c(prev, 2), c(p + 10, 2), c(p - 10, 2), c(p, 2), sign,
                             c(abs(p - prev), 2), c(p, 2), "1", c(p, 2), "1", "20.00"])
            tables = [{"title": f"{d} 價格指數(臺灣證券交易所)", "fields": IDX_FIELDS,
                       "data": [["寶島股價指數", "50,000.00", "<p style= color:red>+</p>", "100.00", "0.20", ""],
                                ["發行量加權股價指數", "44,257.15", "<p style= color:red>+</p>", "1,545.23", "3.62", ""]]}]
            tables += [{"title": "大盤統計資訊", "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"], "data": []}]
            tables += [{"title": f"{d} 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))", "fields": QUOTE_FIELDS, "data": rows}]
            return 200, json.dumps({"tables": tables, "stat": "OK", "date": d}).encode()
        if path.endswith("/T86"):
            rows = []
            for code, (name, mkt, _) in self.stocks.items():
                if mkt != "TWSE":
                    continue
                f = 1_000_000 if i % 3 else -500_000
                t = 200_000
                rows.append([code, name, "0", "0", c(f), "0", "0", "0", "0", "0", c(t), c(50_000), "0", "0", "0", "0", "0", "0", c(f + t + 50_000)])
            return 200, json.dumps({"stat": "OK", "date": d, "fields": T86_FIELDS, "data": rows}).encode()
        if path.endswith("/MI_MARGN"):
            if d in self.no_margin:
                return 200, json.dumps(nodata).encode()
            rows = []
            for code, (name, mkt, _) in self.stocks.items():
                if mkt == "TWSE":
                    rows.append([code, name, "3,201", "2,682", "43", c(34596 + i), c(34596 + i - 100), "6,483,092", "21", "49", "0", "47", "75", "6,483,092", "35", ""])
            return 200, json.dumps({"stat": "OK", "date": d, "tables": [
                {"title": "信用交易統計", "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"], "data": []},
                {"title": "融資融券彙總 (全部)", "fields": MARGN_FIELDS, "data": rows}]}).encode()
        if path.endswith("/BFI82U"):
            return 200, json.dumps({"stat": "OK", "date": d, "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"], "data": [
                ["自營商(自行買賣)", "1", "1", "-4,309,304,258"], ["自營商(避險)", "1", "1", "-45,166,631,246"],
                ["投信", "1", "1", "26,691,208,730"], ["外資及陸資(不含外資自營商)", "1", "1", "-91,188,249,259"],
                ["外資自營商", "0", "0", "0"], ["合計", "1", "1", "-113,972,976,033"]]}).encode()
        # ---- TPEx（新版 tables 格式）----
        code, (name, _, _) = "6488", self.stocks["6488"]
        prev, p, v = self.px[(code, d)]
        if path.endswith("stk_quote_result.php"):  # 模擬實機：舊網址忽略日期參數，永遠回傳最新一天
            d = self.days[-1]
            prev, p, v = self.px[(code, d)]
        if (path.endswith("/otc") or path.endswith("stk_wn1430_result.php")) and not self.otc_ok:
            return 404, b"not found"
        if path.endswith("/otc") or path.endswith("stk_quote_result.php"):
            return 200, json.dumps({"stat": "ok", "date": d, "tables": [{"title": "上櫃股票行情", "date": d,
                "fields": ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數", "成交金額(元)", "成交筆數"],
                "data": [[code, name, c(p, 2), f"{p - prev:+.2f}", c(prev, 2), c(p + 5, 2), c(p - 5, 2), c(p, 2), c(v), c(v * p), "100"]]}]}).encode()
        if path.endswith("/dailyTrade"):
            row = [code, name] + ["0"] * 22
            row[4], row[13], row[22], row[23] = c(300_000), c(-100_000), c(10_000), c(210_000)
            return 200, json.dumps({"stat": "ok", "date": d, "tables": [{"title": "三大法人", "date": d,
                "fields": ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"], "data": [row]}]}).encode()
        if path.endswith("/balance"):
            if d in self.no_margin:
                return 200, json.dumps({"stat": "ok", "date": d, "tables": [{"date": d, "fields": [], "data": []}]}).encode()
            return 200, json.dumps({"stat": "ok", "date": d, "tables": [{"date": d,
                "fields": ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率(%)", "資限額",
                           "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率(%)", "券限額", "資券相抵(張)", "備註"],
                "data": [[code, name, "9,612", "1", "1", "0", "9,653", "0", "1", "1", "202", "1", "1", "0", "195", "0", "0", "0", "0", ""]]}]}).encode()
        return 404, b"{}"


def start_fake(fx: FakeExchange):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            code, body = fx.handle(u.path, parse_qs(u.query))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    S.TWSE = base + "/twse"
    S.TPEX = base + "/tpex"
    S.FINMIND = base + "/finmind"
    return srv


def weekdays(n, end="20261111"):
    return sorted(S.weekdays_back(end, n))


class ParserTests(unittest.TestCase):
    def test_num_and_sign(self):
        from twradar.net import num
        self.assertEqual(num("1,234.50"), 1234.5)
        self.assertIsNone(num("--"))
        self.assertEqual(S._sign('<p style= color:green>-</p>'), -1)
        self.assertEqual(S._sign('<p>X</p>'), 0)
        self.assertEqual(S.norm_date("115/11/06"), "20261106")
        self.assertEqual(S.roc("20261106"), "115/11/06")


class EngineTests(unittest.TestCase):
    """用 2026/11/11 實際數字，結果需與晨報一致。"""

    def light(self, rec, stock=None):
        return evaluate(rec, stock or {}, DEFAULT_RULES)["light"]

    def test_tsmc_wait_volume(self):
        r = {"close": 2395, "chgPct": 4.81, "vol": 30370, "vol5": 58445.6, "ma20": 2362.75, "prevHigh": 2540,
             "foreign": 6038, "trust": 2385, "foreignStreak": 1, "trustStreak": 5}
        e = evaluate(r, {}, DEFAULT_RULES)
        self.assertEqual(e["light"], "wait")
        self.assertIn("量能", e["needs"][0])

    def test_mediatek_near_ma_not_red(self):
        r = {"close": 3990, "chgPct": 4.18, "vol": 10733.6, "vol5": 16606.8, "ma20": 3991.5, "foreign": -426,
             "trust": 112, "foreignStreak": -6, "trustStreak": 6}
        e = evaluate(r, {"key_level": 4000}, DEFAULT_RULES)
        self.assertEqual(e["light"], "wait")
        self.assertIn("尚未站上", e["keyLevelNote"])

    def test_divergence_red(self):
        r = {"close": 4500, "chgPct": 3.09, "vol": 3933, "vol5": 5544, "ma20": 4772.5, "foreign": -1078, "trust": 55, "marginChg": 245}
        e = evaluate(r, {}, DEFAULT_RULES)
        self.assertEqual(e["light"], "stop")
        self.assertTrue(any("背離" in w for w in e["warnings"]))

    def test_green(self):
        r = {"close": 110, "chgPct": 2, "vol": 200, "vol5": 100, "ma20": 100, "foreign": 50, "trust": 10,
             "foreignStreak": 3, "trustStreak": 1, "marginChg": -10}
        self.assertEqual(self.light(r), "go")

    def test_no_data(self):
        self.assertEqual(self.light({}), "na")


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = weekdays(70)
        cls.holiday = cls.days[-5]
        cls.fx = FakeExchange([d for d in cls.days], holiday={cls.holiday}, no_margin={cls.days[-1]})
        cls.srv = start_fake(cls.fx)
        from twradar.cli import open_db
        from twradar.server import App, make_handler
        cls.cfg = load_config()
        cls.cfg.request_interval = 0
        cls.cfg.port = 0
        cls.db = open_db(cls.cfg)
        # 只留下假交易所有的三檔
        for w in cls.db.watchlist():
            if w["code"] not in ("2330", "2454", "6488"):
                cls.db.delete_stock(w["code"])
        cls.app = App(cls.cfg, cls.db, start_background=False)
        cls.app.crawler.f = Fetcher(0, "test")
        cls.api_srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.api_srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.api_srv.server_address[1]}"
        for d in cls.days[:-1]:
            cls.app.crawler.crawl_day(d, final=True, analyze=False)
        cls.last = cls.days[-1]
        cls.run_last = cls.app.crawler.crawl_day(cls.last, final=False)

    def get(self, path, token=True):
        url = self.base + path + (("&" if "?" in path else "?") + "token=" + self.cfg.token if token else "")
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_holiday_detected(self):
        self.assertEqual(self.db.get_run(self.holiday)["status"], "holiday")

    def test_complete_day(self):
        d = self.days[-2]
        run = self.db.get_run(d)
        self.assertEqual(run["status"], "complete", run)

    def test_a_waiting_when_margin_missing(self):
        self.assertEqual(self.run_last["status"], "waiting")
        st, body = self.get(f"/api/v1/days/{self.last}")
        env = json.loads(body)
        self.assertEqual(env["status"], "waiting")
        self.assertFalse(env["ready"])
        self.assertEqual(env["analysis"]["status"], "provisional")

    def test_indicators_ma20(self):
        d = self.days[-2]
        from twradar.analysis import indicators
        rec = indicators(self.db, "2330", d)
        trading = [x for x in self.days if x != self.holiday and x <= d]
        closes = [self.fx.px[("2330", x)][1] for x in trading[-20:]]
        self.assertAlmostEqual(rec["ma20"], round(sum(closes) / 20, 2), places=2)
        self.assertIn("ma60", rec)
        self.assertEqual(rec["marginChg"], -100)
        self.assertEqual(rec["trust"], 200)

    def test_tpex_parsed(self):
        d = self.days[-2]
        from twradar.analysis import indicators
        rec = indicators(self.db, "6488", d)
        self.assertEqual(rec["foreign"], 300)
        self.assertEqual(rec["trust"], -100)
        self.assertEqual(rec["marginChg"], 41)

    def test_final_then_complete_and_api(self):
        self.fx.no_margin = set()
        run = self.app.crawler.crawl_day(self.last, final=True)
        self.assertEqual(run["status"], "complete")
        st, body = self.get("/api/v1/latest?ready=1")
        env = json.loads(body)
        self.assertTrue(env["ready"])
        self.assertEqual(env["analysis"]["status"], "complete")
        self.assertEqual(len(env["stocks"]), 3)
        self.assertIn("light", env["stocks"][0]["signal"])
        st, csvb = self.get(f"/api/v1/days/{self.last}?format=csv")
        self.assertEqual(st, 200)
        self.assertTrue(csvb.decode("utf-8-sig").startswith("code,name"))
        st, rb = self.get(f"/api/v1/days/{self.last}?format=radar")
        self.assertIn(self.last, json.loads(rb)["days"][0]["date"])
        st, hb = self.get("/api/v1/stocks/2330/history?days=30")
        self.assertEqual(json.loads(hb)["days"], 30)

    def test_auth_required(self):
        st, _ = self.get("/api/v1/latest", token=False)
        self.assertEqual(st, 401)
        st, _ = self.get("/api/v1/health", token=False)
        self.assertEqual(st, 200)

    def test_ui_served(self):
        st, body = self.get("/", token=False)
        self.assertEqual(st, 200)
        self.assertIn("右側加碼雷達", body.decode())

    def test_sqlite_export(self):
        st, body = self.get("/api/v1/export/sqlite")
        self.assertEqual(st, 200)
        self.assertTrue(body.startswith(b"SQLite format 3"))


class FallbackTests(unittest.TestCase):
    def test_tpex_blocked_uses_finmind(self):
        days = weekdays(3, "20261106")
        fx = FakeExchange(days, block_tpex=True)
        start_fake(fx)
        os.environ["TWRADAR_HOME"] = tempfile.mkdtemp()
        from twradar.cli import open_db
        from twradar.jobs import Crawler
        cfg = load_config()
        db = open_db(cfg)
        for w in db.watchlist():
            if w["code"] not in ("2330", "2454", "6488"):
                db.delete_stock(w["code"])
        cr = Crawler(db, cfg)
        cr.f = Fetcher(0, "test")
        run = cr.crawl_day(days[-1], final=True)
        self.assertEqual(run["steps"]["tpex_quotes"]["status"], "fallback", run["steps"]["tpex_quotes"])
        self.assertEqual(run["status"], "complete")
        os.environ["TWRADAR_HOME"] = TMP


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TpexDateIgnoredTests(unittest.TestCase):
    def test_ignored_date_triggers_fallback(self):
        days = weekdays(5, "20261111")
        fx = FakeExchange(days, otc_ok=False)
        start_fake(fx)
        os.environ["TWRADAR_HOME"] = tempfile.mkdtemp()
        from twradar.cli import open_db
        from twradar.jobs import Crawler
        cfg = load_config()
        db = open_db(cfg)
        for w in db.watchlist():
            if w["code"] not in ("2330", "2454", "6488"):
                db.delete_stock(w["code"])
        cr = Crawler(db, cfg)
        cr.f = Fetcher(0, "test")
        with self.assertRaises(S.FetchError) as cm:
            S.tpex_quotes(cr.f, days[-3])
        self.assertEqual(cm.exception.kind, "parse")
        run = cr.crawl_day(days[-3], final=False)
        self.assertEqual(run["steps"]["tpex_quotes"]["status"], "fallback")
        # 當天（網站最新一天）直接用官方資料
        self.assertEqual(len(S.tpex_quotes(cr.f, days[-1])), 1)
        os.environ["TWRADAR_HOME"] = TMP
