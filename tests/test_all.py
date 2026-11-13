"""完整測試：解析器（用實際格式）、訊號判定、端到端（假交易所伺服器 → 抓取 → 分析 → API）。

執行：python -m unittest discover -s tests -v
"""
import io
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

from twradar import dates as D  # noqa: E402
from twradar.providers import build  # noqa: E402
from twradar.providers.common import sign  # noqa: E402
from twradar.analysis import DEFAULT_RULES, evaluate  # noqa: E402
from twradar.config import load_config  # noqa: E402
from twradar.net import FetchError, Fetcher  # noqa: E402

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
            st, en = D.norm_date(qs["start_date"][0]), D.norm_date(qs["end_date"][0])
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
        d = D.norm_date(d) if d else ""
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
    return srv, {"twse": {"base": base + "/twse"}, "tpex": {"base": base + "/tpex"}, "finmind": {"base": base + "/finmind"}}


def setup_env(opts, codes=("2330", "2454", "6488"), home=None):
    """建立獨立的資料夾、設定與資料庫，資料源指向假交易所（依賴注入，不改模組全域變數）。"""
    os.environ["TWRADAR_HOME"] = home or tempfile.mkdtemp()
    from twradar.cli import open_all
    cfg = load_config()
    cfg.request_interval = 0
    db, st = open_all(cfg)

    def fn(x):
        x["watchlist"] = [w for w in x["watchlist"] if w["code"] in codes]
        x["sources"] = {"primary": ["twse", "tpex"], "fallback": ["finmind"], "options": opts}
    st.update(fn)
    os.environ["TWRADAR_HOME"] = TMP
    return cfg, db, st


def weekdays(n, end="20261111"):
    return sorted(D.weekdays_back(end, n))


class ParserTests(unittest.TestCase):
    def test_num_and_sign(self):
        from twradar.net import num
        self.assertEqual(num("1,234.50"), 1234.5)
        self.assertIsNone(num("--"))
        self.assertEqual(sign('<p style= color:green>-</p>'), -1)
        self.assertEqual(sign('<p>X</p>'), 0)
        self.assertEqual(D.norm_date("115/11/06"), "20261106")
        self.assertEqual(D.roc("20261106"), "115/11/06")


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
        e = evaluate(r, {"keyLevel": 4000}, DEFAULT_RULES)
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
        cls.srv, opts = start_fake(cls.fx)
        from twradar.server import App, make_handler
        cls.cfg, cls.db, cls.st = setup_env(opts, home=TMP)  # 只留下假交易所有的三檔
        cls.cfg.port = 0
        cls.app = App(cls.cfg, cls.db, cls.st, start_background=False)
        cls.app.crawler.f = Fetcher(0, "test")  # 測試替換 transport 後資料源要跟著換
        cls.api_srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.api_srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.api_srv.server_address[1]}"
        for d in cls.days[:-1]:
            cls.app.crawler.crawl_day(d, final=True, analyze=False)
        cls.last = cls.days[-1]
        cls.run_last = cls.app.crawler.crawl_day(cls.last, final=False)

    def get(self, path, token=True, headers=None, method="GET", body=None):
        url = self.base + path + (("&" if "?" in path else "?") + "token=" + self.cfg.read_token if token else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def admin(self):
        return {"Authorization": "Bearer " + self.cfg.admin_token}

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
        st, _ = self.get("/api/v1/export/sqlite")
        self.assertEqual(st, 403)  # 唯讀 token 不能匯出
        st, body = self.get("/api/v1/export/sqlite", token=False, headers=self.admin())
        self.assertEqual(st, 200)
        self.assertTrue(body.startswith(b"SQLite format 3"))


class FallbackTests(unittest.TestCase):
    def test_tpex_blocked_uses_finmind(self):
        days = weekdays(3, "20261106")
        fx = FakeExchange(days, block_tpex=True)
        _, opts = start_fake(fx)
        from twradar.jobs import Crawler
        cfg, db, st = setup_env(opts)
        cr = Crawler(db, cfg, st, transport=Fetcher(0, "test"))
        run = cr.crawl_day(days[-1], final=True)
        self.assertEqual(run["steps"]["tpex_quotes"]["status"], "fallback", run["steps"]["tpex_quotes"])
        self.assertEqual(run["status"], "complete")


class ProviderDITests(unittest.TestCase):
    """資料源可注入：不經 HTTP 的記憶體資料源也能跑完整流程。"""

    def _db(self):
        return setup_env({}, codes=("2330",))

    def test_in_memory_provider(self):
        from twradar.jobs import Crawler
        from twradar.providers import Provider

        class Mem(Provider):
            id, label, markets, datasets = "mem", "記憶體", ("TWSE",), ("quotes", "inst")

            def fetch(self, dataset, d):
                if dataset == "quotes":
                    return {"quotes": [{"date": d, "code": "2330", "name": "台積電", "market": "TWSE", "close": 100.0,
                                        "change": 1.0, "volume": 1000, "src": "mem"}]}
                return {"inst": [{"date": d, "code": "2330", "foreign_net": 5000, "trust_net": 0, "src": "mem"}]}

        cfg, db, st = self._db()
        cr = Crawler(db, cfg, st, transport=None, primary=[Mem(None)], fallback=[])
        run = cr.crawl_day("20261111", final=True)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(set(run["steps"]), {"mem_quotes", "mem_inst"})
        from twradar.analysis import indicators
        rec = indicators(db, "2330", "20261111")
        self.assertEqual((rec["close"], rec["foreign"], rec["source"]), (100.0, 5, "mem"))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            build(["nope"], Fetcher(0, "t"))


    def test_doctor_against_fake(self):
        from twradar.cli import doctor
        days = weekdays(3, "20261111")
        _, opts = start_fake(FakeExchange(days))
        cfg, _, st = setup_env(opts)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor(cfg, days[-1], transport=Fetcher(0, "test"), settings=st)
        self.assertEqual(code, 0, out.getvalue())


class SettingsTests(unittest.TestCase):
    def store(self):
        from twradar.settings import SettingsStore
        return SettingsStore(Path(tempfile.mkdtemp()) / "settings.json", 50)

    def test_validate_reports_paths(self):
        from twradar.settings import SettingsError
        st = self.store()
        bad = st.get()
        bad["watchlist"][1]["code"] = bad["watchlist"][0]["code"]
        bad["watchlist"][2]["code"] = "23"
        bad["sources"]["primary"] = ["finmind"]
        bad["display"]["colors"]["up"] = "red"
        bad["schedule"] = ["25:00"]
        before = st.path.read_text(encoding="utf-8")
        with self.assertRaises(SettingsError) as cm:
            st.save(bad)
        paths = {e["path"] for e in cm.exception.errors}
        for want in ("watchlist[1].code", "watchlist[2].code", "sources.primary[0]", "display.colors.up", "schedule[0]"):
            self.assertIn(want, paths)
        self.assertEqual(st.path.read_text(encoding="utf-8"), before)  # 不合法不寫入
        # 內建參數被刪掉時自動補回預設值，不會讓判定少一個門檻
        ok = st.get()
        ok["params"] = [p for p in ok["params"] if p["id"] != "maTol"]
        self.assertIn("maTol", {p["id"] for p in st.save(ok)["params"]})

    def test_history_and_restore(self):
        st = self.store()
        st.update(lambda s: s.__setitem__("confirmDays", 2))
        st.update(lambda s: s.__setitem__("confirmDays", 3))
        h = st.history()
        self.assertEqual(len(h), 2)
        st.restore(h[0]["id"])  # 最新的歷史版 = confirmDays 2
        self.assertEqual(st.get()["confirmDays"], 2)
        for i in range(25):
            st.update(lambda s, i=i: s.__setitem__("schedule", [f"17:{i:02d}"]))
        self.assertEqual(len(st.history()), 20)

    def test_manual_edit_reload_and_invalid_kept(self):
        st = self.store()
        data = json.loads(st.path.read_text(encoding="utf-8"))
        data["confirmDays"] = 4
        st.path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(st.path, (1, 1))
        self.assertEqual(st.get()["confirmDays"], 4)
        st.path.write_text("{ not json", encoding="utf-8")
        os.utime(st.path, (2, 2))
        self.assertEqual(st.get()["confirmDays"], 4)  # 沿用上一個合法版本
        self.assertIsNotNone(st.last_error)

    def test_normalize_adds_new_builtin_params(self):
        from twradar.settings import BUILTIN_PARAMS, SettingsStore
        st = self.store()
        data = json.loads(st.path.read_text(encoding="utf-8"))
        data["params"] = data["params"][:2]
        data["params"][0]["value"] = 0.8
        st.path.write_text(json.dumps(data), encoding="utf-8")
        st2 = SettingsStore(st.path, 50)
        ids = [p["id"] for p in st2.get()["params"]]
        self.assertEqual(set(ids), {b["id"] for b in BUILTIN_PARAMS})
        self.assertEqual(st2.params()["maTol"], 0.8)

    def test_mask_roundtrip(self):
        from twradar.settings import mask
        st = self.store()
        st.update(lambda s: s["sources"]["options"].__setitem__("finmind", {"token": "secret-abcd1234"}))
        shown = mask(st.get())
        self.assertEqual(shown["sources"]["options"]["finmind"]["token"], "••••1234")
        st.save(shown)  # 網頁送回遮蔽值 = 不變
        self.assertEqual(st.get()["sources"]["options"]["finmind"]["token"], "secret-abcd1234")

    def test_migrate_from_v01(self):
        home = tempfile.mkdtemp()
        os.environ["TWRADAR_HOME"] = home
        try:
            from twradar.db import DB
            db = DB(Path(home) / "twradar.sqlite3")
            with db.tx() as c:
                c.execute("INSERT INTO stocks(code,name,market,grp,note,key_level,sort_order) VALUES('2454','聯發科','TWSE','半導體','n',4000,2)")
                c.execute("INSERT INTO stocks(code,name,market,grp,note,key_level,sort_order) VALUES('6488','環球晶','TPEx','','',NULL,1)")
            db.set_setting("seeded", True)
            db.set_setting("rules", {"maTol": 1.0, "blowoff": 2})
            (Path(home) / "config.json").write_text(json.dumps({
                "port": 9000, "token": "OLDTOKEN", "schedule": ["18:00", "07:00"], "finmind_token": "fm-9999",
                "use_finmind_fallback": True}), encoding="utf-8")
            from twradar.cli import open_all
            cfg = load_config()
            self.assertEqual(cfg.read_token, "OLDTOKEN")  # 晨報網址不用改
            db2, st = open_all(cfg)
            s = st.get()
            self.assertEqual([w["code"] for w in s["watchlist"]], ["6488", "2454"])
            self.assertEqual(s["watchlist"][1]["keyLevel"], 4000)
            self.assertEqual(s["watchlist"][1]["group"], "半導體")
            self.assertEqual((st.params()["maTol"], st.params()["blowoff"]), (1.0, 2))
            self.assertEqual(s["schedule"], ["18:00", "07:00"])
            self.assertEqual(s["sources"]["options"]["finmind"]["token"], "fm-9999")
            saved = json.loads((Path(home) / "config.json").read_text(encoding="utf-8"))
            for k in ("token", "schedule", "finmind_token", "use_finmind_fallback"):
                self.assertNotIn(k, saved)
            self.assertEqual(saved["port"], 9000)
            self.assertTrue(saved["admin_token"] and saved["admin_password_hash"] and saved["admin_initial_password"])
            # 第二次啟動不會重複遷移
            cfg2 = load_config()
            self.assertEqual(cfg2.read_token, "OLDTOKEN")
            self.assertEqual(open_all(cfg2)[1].get()["schedule"], ["18:00", "07:00"])
        finally:
            os.environ["TWRADAR_HOME"] = TMP

    def test_crawler_follows_source_changes(self):
        from twradar.jobs import Crawler
        cfg, db, st = setup_env({})
        cr = Crawler(db, cfg, st, transport=Fetcher(0, "t"))
        self.assertIn("tpex_quotes", cr.step_ids)
        st.update(lambda s: s["sources"].__setitem__("primary", ["twse"]))
        cr.refresh()
        self.assertNotIn("tpex_quotes", cr.step_ids)


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        from twradar.server import App, make_handler
        self.cfg, self.db, self.st = setup_env({}, codes=("2330",))
        self.app = App(self.cfg, self.db, self.st, start_background=False)
        self.app.worker.submit = lambda *a, **k: True  # 不在背景補歷史
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def login(self, pw):
        st, js, h = self.call("/api/v1/auth/login", "POST", {"password": pw})
        cookie = (h.get("Set-Cookie") or "").split(";")[0]
        return st, {"Cookie": cookie} if cookie else {}

    def test_roles(self):
        R = {"Authorization": "Bearer " + self.cfg.read_token}
        A = {"Authorization": "Bearer " + self.cfg.admin_token}
        self.assertEqual(self.call("/api/v1/watchlist", headers=R)[0], 200)
        self.assertEqual(self.call("/api/v1/watchlist", "POST", {"code": "2454", "name": "聯發科"}, R)[0], 403)
        self.assertEqual(self.call("/api/v1/settings", headers=R)[0], 403)
        st, js, _ = self.call("/api/v1/watchlist", "POST", {"code": "2454", "name": "聯發科", "holding": True}, A)
        self.assertEqual(st, 200, js)
        self.assertTrue(self.st.watchlist()[-1]["holding"])
        # 舊版網頁更新時不送 holding，不能被清掉
        self.call("/api/v1/watchlist", "POST", {"code": "2454", "name": "聯發科", "note": "x"}, A)
        self.assertTrue(self.st.watchlist()[-1]["holding"])
        self.assertEqual(self.call("/api/v1/latest?token=" + self.cfg.admin_token)[0], 400)  # 管理 token 不可放網址
        self.assertEqual(self.call("/api/v1/latest")[0], 401)

    def test_login_cookie_password_change(self):
        self.assertEqual(self.login("wrong-password")[0], 401)
        st, ck = self.login(self.cfg.admin_initial_password)
        self.assertEqual(st, 200)
        me = self.call("/api/v1/auth/me", headers=ck)[1]
        self.assertEqual((me["role"], me["initialPassword"]), ("admin", True))
        # 網頁同時帶唯讀 token 與登入 cookie：以管理者為準
        both = {**ck, "Authorization": "Bearer " + self.cfg.read_token}
        self.assertEqual(self.call("/api/v1/auth/me", headers=both)[1]["role"], "admin")
        st, toks, _ = self.call("/api/v1/auth/tokens", headers=ck)
        self.assertEqual(toks["readToken"], self.cfg.read_token)  # 管理者查得到 token
        self.assertEqual(self.call("/api/v1/settings", headers=ck)[0], 200)
        self.assertEqual(self.call("/api/v1/auth/password", "POST", {"current": "nope", "new": "12345678"}, ck)[0], 403)
        self.assertEqual(self.call("/api/v1/auth/password", "POST", {"current": self.cfg.admin_initial_password, "new": "short"}, ck)[0], 400)
        st, _, h = self.call("/api/v1/auth/password", "POST", {"current": self.cfg.admin_initial_password, "new": "my-own-pass"}, ck)
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/v1/settings", headers=ck)[0], 401)  # 舊 cookie 失效
        new_ck = {"Cookie": h.get("Set-Cookie").split(";")[0]}
        self.assertEqual(self.call("/api/v1/settings", headers=new_ck)[0], 200)
        self.assertEqual(self.cfg.admin_initial_password, "")
        self.assertEqual(self.login("my-own-pass")[0], 200)
        st, _, h = self.call("/api/v1/auth/logout", "POST", {}, new_ck)
        self.assertIn("Max-Age=0", h.get("Set-Cookie"))

    def test_lockout(self):
        for _ in range(5):
            self.login("bad-password")
        self.assertEqual(self.login(self.cfg.admin_initial_password)[0], 429)

    def test_rotate_token(self):
        A = {"Authorization": "Bearer " + self.cfg.admin_token}
        old = self.cfg.read_token
        st, js, _ = self.call("/api/v1/auth/rotate", "POST", {"kind": "read"}, A)
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/v1/latest?token=" + old)[0], 401)
        self.assertEqual(self.call("/api/v1/latest?token=" + js["token"])[0], 200)
        saved = json.loads((self.cfg.settings_path.parent / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["read_token"], js["token"])

    def test_settings_api(self):
        A = {"Authorization": "Bearer " + self.cfg.admin_token}
        st, js, _ = self.call("/api/v1/settings", headers=A)
        s = js["settings"]
        s["watchlist"][0]["code"] = "x"
        st, js, _ = self.call("/api/v1/settings", "PUT", {"settings": s}, A)
        self.assertEqual(st, 400)
        self.assertEqual(js["errors"][0]["path"], "watchlist[0].code")
        s["watchlist"][0]["code"] = "2330"
        s["confirmDays"] = 2
        n0 = len(self.st.history())
        self.assertEqual(self.call("/api/v1/settings", "PUT", {"settings": s}, A)[0], 200)
        self.assertEqual(len(self.call("/api/v1/settings/history", headers=A)[1]), n0 + 1)
        self.assertEqual(self.call("/api/v1/settings/reset", "POST", {"section": "confirmDays"}, A)[0], 200)
        self.assertEqual(self.st.get()["confirmDays"], 1)
        st, js, _ = self.call("/api/v1/rules", "PUT", {"maTol": 1.2}, A)
        self.assertEqual(js["rules"]["maTol"], 1.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TpexDateIgnoredTests(unittest.TestCase):
    def test_ignored_date_triggers_fallback(self):
        days = weekdays(5, "20261111")
        fx = FakeExchange(days, otc_ok=False)
        _, opts = start_fake(fx)
        from twradar.jobs import Crawler
        cfg, db, st = setup_env(opts)
        cr = Crawler(db, cfg, st, transport=Fetcher(0, "test"))
        tpex = build(["tpex"], cr.f, opts)[0]
        with self.assertRaises(FetchError) as cm:
            tpex.fetch("quotes", days[-3])
        self.assertEqual(cm.exception.kind, "parse")
        run = cr.crawl_day(days[-3], final=False)
        self.assertEqual(run["steps"]["tpex_quotes"]["status"], "fallback")
        # 當天（網站最新一天）直接用官方資料
        self.assertEqual(len(tpex.fetch("quotes", days[-1])["quotes"]), 1)
