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
                if d not in self.holiday:  # 休市日價格不變（漲跌是相對前一個交易日）
                    p = round(base * (1 + 0.002 * i) + (5 if i % 2 else -3), 1)
                self.px[(code, d)] = (prev, p, 10_000_000 + i * 100_000)

    taiex: dict = {}      # 加權指數（預設固定 44,257.15）
    flows: dict = {}      # {(代號, 日期): (外資, 投信)} 買賣超股數；預設固定模式
    tdcc_date = None      # 集保資料日（預設最後一天）

    def set_path(self, code, closes, vol=10_000_000):
        """指定某檔的收盤路徑（開盤＝前收、最高＝收＋10、最低＝收－10）。"""
        prev = closes[0]
        for d, p in zip(self.days, closes):
            self.px[(code, d)] = (prev, p, vol)
            prev = p
    tdcc_whale = 80.0     # 千張以上大戶持股比例
    tdcc_retail = 1000    # 1–999 股的人數（散戶人數用）

    def tdcc_csv(self):
        d = self.tdcc_date or self.days[-1]
        lines = ["資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%"]
        total = 1_000_000_000
        for code in self.stocks:
            rest = (100 - self.tdcc_whale) / 14
            for lv in range(1, 16):
                pct = self.tdcc_whale if lv == 15 else rest
                people = self.tdcc_retail if lv == 1 else 100
                lines.append(f"{d},{code},{lv},{people},{int(total * pct / 100)},{pct:.2f}")
            lines.append(f"{d},{code},16,0,0,0.00")
            lines.append(f"{d},{code},17,{self.tdcc_retail + 1400},{total},100.00")
        lines.append(f"{d},9999,1,1,1,100.00")  # 不在觀察清單：不應存入
        return "\n".join(lines).encode("utf-8-sig")

    def handle(self, path, qs):
        if "/tdcc/" in path:
            return 200, self.tdcc_csv()
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
                                ["發行量加權股價指數", c(self.taiex.get(d, 44257.15), 2), "<p style= color:red>+</p>", "1,545.23", "3.62", ""]]}]
            tables += [{"title": "大盤統計資訊", "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"], "data": []}]
            tables += [{"title": f"{d} 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))", "fields": QUOTE_FIELDS, "data": rows}]
            return 200, json.dumps({"tables": tables, "stat": "OK", "date": d}).encode()
        if path.endswith("/T86"):
            rows = []
            for code, (name, mkt, _) in self.stocks.items():
                if mkt != "TWSE":
                    continue
                f, t = self.flows.get((code, d), (1_000_000 if i % 3 else -500_000, 200_000))
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
    return srv, {"twse": {"base": base + "/twse"}, "tpex": {"base": base + "/tpex"}, "finmind": {"base": base + "/finmind"},
                 "tdcc": {"base": base + "/tdcc"}}


def setup_env(opts, codes=("2330", "2454", "6488"), home=None):
    """建立獨立的資料夾、設定與資料庫，資料源指向假交易所（依賴注入，不改模組全域變數）。"""
    os.environ["TWRADAR_HOME"] = home or tempfile.mkdtemp()
    from twradar.cli import open_all
    cfg = load_config()
    cfg.home = os.environ["TWRADAR_HOME"]  # 測試用：記下資料夾
    cfg.request_interval = 0
    db, st = open_all(cfg)

    def fn(x):
        x["watchlist"] = [w for w in x["watchlist"] if w["code"] in codes]
        x["sources"] = {"primary": ["twse", "tpex"], "fallback": ["finmind"],
                        "weekly": ["tdcc"] if "tdcc" in opts else [], "options": opts}
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


def _v013_settings():
    """v0.1.3 的規則（沒有「法人對作」），用於對照。"""
    from twradar.rules import p3_playbooks
    pb = p3_playbooks()
    pb["entry"][1]["when"]["any"].remove("instConflict")
    return {"playbooks": pb, "conditions": []}


def _p3_settings():
    from twradar.rules import p3_playbooks
    return {"playbooks": p3_playbooks(), "conditions": []}


V013 = _v013_settings()
P3 = _p3_settings()
P3_RULES = {**DEFAULT_RULES, "conflictMinAmt": 1000, "profitTargetPct": 20, "limitNearPct": 9, "whaleAmount": 0}


class EngineTests(unittest.TestCase):
    """v0.2 P3 規則（v0.1.3 判定＋法人對作）的行為；v0.3 規則見 V3RulesTests。"""
    """用 2026/11/11 實際數字，結果需與晨報一致。"""

    def light(self, rec, stock=None):
        return evaluate(rec, stock or {}, P3_RULES, P3)["light"]

    def test_tsmc_wait_volume(self):
        r = {"close": 2395, "chgPct": 4.81, "vol": 30370, "vol5": 58445.6, "ma20": 2362.75, "prevHigh": 2540,
             "foreign": 6038, "trust": 2385, "foreignStreak": 1, "trustStreak": 5}
        e = evaluate(r, {}, P3_RULES, P3)
        self.assertEqual(e["light"], "wait")
        self.assertIn("量能", e["needs"][0])

    def test_mediatek_near_ma_not_red(self):
        r = {"close": 3990, "chgPct": 4.18, "vol": 10733.6, "vol5": 16606.8, "ma20": 3991.5, "foreign": -426,
             "trust": 112, "foreignStreak": -6, "trustStreak": 6}
        e = evaluate(r, {"keyLevel": 4000}, P3_RULES, V013)
        self.assertEqual(e["light"], "wait")
        self.assertIn("尚未站上", e["keyLevelNote"])
        # v0.2 新增「法人對作」：外資賣 426、投信買 112 張（約 4.5 億）→ 不宜加碼
        e = evaluate(r, {"keyLevel": 4000}, P3_RULES, P3)
        self.assertEqual(e["light"], "stop")
        self.assertIn("法人對作", e["warnings"][0])

    def test_divergence_red(self):
        r = {"close": 4500, "chgPct": 3.09, "vol": 3933, "vol5": 5544, "ma20": 4772.5, "foreign": -1078, "trust": 55, "marginChg": 245}
        e = evaluate(r, {}, P3_RULES, P3)
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

    def test_hold_signal_in_api(self):
        self.st.update(lambda x: x["watchlist"][0].update(holding=True, keyLevel=99999.0))
        try:
            self.app.reanalyze_latest()
            st, body = self.get("/api/v1/latest")
            rows = {s["code"]: s for s in json.loads(body)["stocks"]}
            w0 = self.st.watchlist()[0]["code"]
            self.assertEqual(rows[w0]["signal"]["hold"]["rule"], "停損")  # 跌破關鍵價
            self.assertIn("trace", rows[w0]["signal"])
            self.assertTrue(all("hold" not in s["signal"] for c, s in rows.items() if c != w0))
        finally:
            self.st.update(lambda x: x["watchlist"][0].update(holding=False, keyLevel=None))
            self.app.reanalyze_latest()

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

    def test_status_has_issues(self):
        self.db.log("warn", "集保股權分散表抓取失敗：測試")
        st, js, _ = self.call("/api/v1/status", headers={"Authorization": "Bearer " + self.cfg.read_token})
        self.assertEqual(st, 200)
        self.assertIn("集保", js["recentIssues"][0]["message"])
        self.assertIn("集保", js["holdersLog"][0]["message"])
        self.assertIsNone(js["backtest"])

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


class RulesEngineTests(unittest.TestCase):
    """P3 規則引擎。"""

    def test_parity_with_v013(self):
        """預設規則（去掉新增的法人對作）與 v0.1.3 在隨機資料上結果完全相同。"""
        import random
        import legacy_v013 as L
        rnd = random.Random(11)

        def maybe(v, p=0.15):
            return None if rnd.random() < p else v
        keys = ["light", "lightLabel", "checks", "instWhy", "chipsPassed", "needs", "warnings", "missing",
                "volRatio", "maDistPct", "belowMa20", "keyLevelNote"]
        for _ in range(3000):
            close = rnd.uniform(20, 3000)
            rec = {"close": maybe(close), "chgPct": maybe(rnd.uniform(-10, 10)), "vol": maybe(rnd.uniform(0, 5000)),
                   "vol5": maybe(rnd.choice([0, rnd.uniform(1, 5000)])), "ma20": maybe(close * rnd.uniform(0.9, 1.1)),
                   "prevHigh": maybe(close * rnd.uniform(0.9, 1.1)), "foreign": maybe(rnd.randint(-3000, 3000)),
                   "trust": maybe(rnd.randint(-500, 500)), "foreignStreak": maybe(rnd.randint(-6, 6)),
                   "trustStreak": maybe(rnd.randint(-6, 6)), "foreign5d": maybe(rnd.randint(-8000, 8000)),
                   "trust5d": maybe(rnd.randint(-2000, 2000)), "marginChg": maybe(rnd.randint(-500, 500)),
                   "mainForce": maybe(rnd.randint(-5, 5), 0.8), "brokerDiff": maybe(rnd.randint(-5, 5), 0.8)}
            rec = {k: v for k, v in rec.items() if v is not None}
            stock = {"keyLevel": maybe(close * rnd.uniform(0.9, 1.1), 0.6)}
            a, b = L.evaluate(rec, stock, DEFAULT_RULES), evaluate(rec, stock, DEFAULT_RULES, V013)
            for k in keys:
                self.assertEqual(a[k], b[k], (k, rec))

    def test_three_valued_logic(self):
        from twradar.conditions import Ctx
        from twradar.rules import Engine
        eng = Engine([{"id": "p", "label": "p", "left": 1, "op": ">", "right": 0},
                      {"id": "f", "label": "f", "left": 0, "op": ">", "right": 1},
                      {"id": "n", "label": "n", "left": {"field": "close"}, "op": ">", "right": 0}])
        ctx = Ctx({}, {}, DEFAULT_RULES)
        ev = lambda node: eng.ev(node, ctx)
        self.assertEqual(ev({"all": ["p", "n"]}), "na")
        self.assertEqual(ev({"all": ["f", "n"]}), "fail")
        self.assertEqual(ev({"any": ["f", "n"]}), "na")
        self.assertEqual(ev({"any": ["p", "n"]}), "pass")
        self.assertEqual(ev({"atLeast": {"n": 2, "of": ["p", "n", "f"]}}), "na")
        self.assertEqual(ev({"atLeast": {"n": 2, "of": ["p", "f", "f"]}}), "fail")
        self.assertEqual(ev({"none": ["f", "f"]}), "pass")
        self.assertEqual(ev({"not": "n"}), "na")

    HOLD = {"close": 120.0, "open": 119.0, "chgPct": 1.0, "vol": 100, "vol5": 100, "ma20": 110.0, "ma20Prev": 109.0,
            "foreign": 10, "trust": 0, "instNet": 10, "instStreak": 1, "marginChg": 0, "marginStreak": 0,
            "marginPos60": 50, "marginRange60": 5000, "highPct": 1, "upperShadowPct": 10, "high": 121, "low": 118,
            "candle": 1, "prevFromHigh20": -1}

    def hold(self, rec=None, **stock):
        st = {"holding": True, **stock}
        return evaluate({**self.HOLD, **(rec or {})}, st, DEFAULT_RULES)["hold"]

    def test_hold_layers_v3(self):
        self.assertEqual(self.hold(cost=100)["light"], "keep")
        # 停損：虧損 ≥ 10%（最後保險）
        self.assertEqual(self.hold(cost=135)["rule"], "停損")
        # 有效跌破下彎月線 1.5%：不用等法人
        self.assertEqual(self.hold({"close": 108, "ma20Prev": 111}, cost=100)["rule"], "停損")
        # 跌破月線 0.5% ＋ 當日法人賣超（不必連賣 2 日）
        self.assertEqual(self.hold({"close": 109.3, "instNet": -50}, cost=100)["rule"], "停損")
        self.assertEqual(self.hold({"close": 109.3, "instNet": 50}, cost=100)["light"], "keep")
        # 跌破關鍵價
        self.assertEqual(self.hold(keyLevel=125)["light"], "exit")
        # 保本線：最高曾獲利 ≥ 10%，回吐到 ≤ 2%
        h = self.hold({"close": 101.5, "ma20": 100, "ma20Prev": 99, "peakClose": 115}, cost=100, buyDate="2026-10-01")
        self.assertEqual((h["light"], h["rule"]), ("exit", "停損"))
        self.assertTrue(any("保本" in x for x in h["reasons"]))
        # 獲利 ≥ 10% ＋ 籌碼轉弱 2 項 → 獲利保護；3 項 → 獲利出場
        two = {"marginStreak": 3, "marginPos60": 90}
        self.assertEqual(self.hold(two, cost=105)["rule"], "獲利保護")
        self.assertEqual(self.hold({**two, "instStreak": -2}, cost=105)["rule"], "獲利出場")
        self.assertEqual(self.hold(two, cost=115)["light"], "keep")  # 獲利不到 10%
        # 融資偏高防呆：60 日融資變化太小不算
        self.assertEqual(self.hold({**two, "marginRange60": 5}, cost=105)["light"], "keep")
        # 出貨警訊：高檔留長上影且出量（不再要求量縮）
        self.assertEqual(self.hold({"highPct": 7.5, "high": 128, "upperShadowPct": 70, "vol": 160}, cost=100)["rule"], "出貨警訊")
        self.assertEqual(self.hold({"highPct": 7.5, "high": 128, "upperShadowPct": 70, "vol": 80}, cost=100)["light"], "keep")
        # 乖離過大：紅 K 創高不減碼，收黑才減碼
        self.assertEqual(self.hold({"close": 127, "high": 128, "open": 125}, cost=100)["light"], "keep")
        self.assertEqual(self.hold({"close": 127, "high": 128, "open": 128, "candle": -1}, cost=100)["rule"], "出貨警訊")
        # 大型股乖離門檻 10%
        self.assertEqual(self.hold({"close": 122, "open": 123, "candle": -1, "capAmt": 5000}, cost=100)["rule"], "出貨警訊")
        # 高檔爆量長黑＋法人賣
        h = self.hold({"chgPct": -3.5, "vol": 200, "instNet": -30, "candle": -1, "open": 124}, cost=100)
        self.assertEqual(h["rule"], "出貨警訊")
        # 鎖漲停：不出場
        self.assertEqual(self.hold({"highPct": 9.9, "high": 120, "upperShadowPct": 0, "vol": 50}, cost=100)["light"], "keep")
        # 沒持有：沒有持有面
        self.assertNotIn("hold", evaluate(self.HOLD, {"cost": 100}, DEFAULT_RULES))

    def test_conflict_amount_threshold(self):
        r = {"close": 100.0, "chgPct": 1, "vol": 300, "vol5": 100, "ma20": 90, "foreign": 400, "trust": -200,
             "foreignStreak": 3, "marginChg": -1}
        self.assertEqual(evaluate(r, {}, P3_RULES, P3)["light"], "stop")    # 投信賣 200 張 × 100 元 = 2,000 萬
        r.update(close=10.0, ma20=9.0)
        self.assertEqual(evaluate(r, {}, P3_RULES, P3)["light"], "go")      # 200 萬元：雜訊，不算對作

    def test_custom_condition_in_playbook(self):
        from twradar.settings import default_settings
        S = default_settings()
        S["conditions"] = [{"id": "aboveMa60", "label": "站上季線", "left": {"field": "close"}, "op": ">=",
                            "right": {"field": "ma60", "adjPct": "-param:maTol"}}]
        S["playbooks"]["entry"].insert(1, {"light": "noAdd", "label": "季線下", "when": {"not": "aboveMa60"}})
        r = {"close": 95, "ma20": 90, "ma60": 100, "chgPct": 1, "vol": 300, "vol5": 100, "foreign": 1, "foreignStreak": 3}
        e = evaluate(r, {}, DEFAULT_RULES, S)
        self.assertEqual((e["light"], e["rule"]), ("stop", "季線下"))
        r["close"] = 99.6  # 季線 100 × (1 − 0.5%) = 99.5
        self.assertNotEqual(evaluate(r, {}, DEFAULT_RULES, S)["rule"], "季線下")

    def test_rule_validation(self):
        from twradar.settings import SettingsError, SettingsStore
        st = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json", 50)
        s = st.get()
        s["conditions"] = [{"id": "inst", "label": "x", "left": {"field": "nope"}, "op": "=>", "right": {"param": "zz"}}]
        s["playbooks"]["entry"] = [{"light": "watch", "default": True},
                                   {"light": "go", "when": {"atLeast": {"n": 5, "of": ["inst", "ghost"]}}}]
        with self.assertRaises(SettingsError) as cm:
            st.save(s)
        paths = {x["path"] for x in cm.exception.errors}
        for want in ("conditions[0].id", "conditions[0].left.field", "conditions[0].op", "conditions[0].right.param",
                     "playbooks.entry[0]", "playbooks.entry[1].light", "playbooks.entry[1].when.atLeast.n",
                     "playbooks.entry[1].when.atLeast.of[1]", "playbooks.entry"):
            self.assertIn(want, paths)

    def test_old_settings_get_default_rules(self):
        from twradar.settings import SettingsStore
        path = Path(tempfile.mkdtemp()) / "settings.json"
        st = SettingsStore(path, 50)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["playbooks"] = {"entry": [], "hold": []}  # P2 版本的設定
        data["params"] = [p for p in data["params"] if p["id"] != "stopLossPct"]
        path.write_text(json.dumps(data), encoding="utf-8")
        s = SettingsStore(path, 50).get()
        self.assertTrue(s["playbooks"]["entry"] and s["playbooks"]["hold"])
        self.assertIn("stopLossPct", {p["id"] for p in s["params"]})

    def test_confirm_days(self):
        from twradar.analysis import _confirm
        self.assertEqual(_confirm([("wait", "wait")], "go", 2), ("wait", True))
        self.assertEqual(_confirm([("go", "wait"), ("wait", "wait")], "go", 2), ("go", False))
        self.assertEqual(_confirm([("go", "go")], "go", 3), ("go", False))
        self.assertEqual(_confirm([], "stop", 2), ("stop", False))
        self.assertEqual(_confirm([("wait", "wait")], "go", 1), ("go", False))

    def test_new_indicators(self):
        from twradar.analysis import _indicators
        days = sorted(D.weekdays_back("20261111", 25), reverse=True)
        q = [{"date": d, "open": 100, "high": 110, "low": 99, "close": 102 if i == 0 else 100, "change": 2 if i == 0 else 0,
              "volume": 1000} for i, d in enumerate(days)]
        ins = [{"date": d, "foreign_net": -1000 if i < 3 else 5000, "trust_net": 0, "dealer_net": 0} for i, d in enumerate(days)]
        mh = [{"date": d, "margin_prev": 100, "margin_bal": 100 + (30 - i if i < 2 else -1), "short_prev": 0, "short_bal": 0}
              for i, d in enumerate(days)]
        rec = _indicators(q, ins, mh[0], None, days[0], mh)
        self.assertEqual(rec["instStreak"], -3)
        self.assertEqual(rec["marginStreak"], 2)
        self.assertEqual(rec["marginPos60"], 100.0)
        self.assertEqual(rec["highPct"], 10.0)                    # 110 / 100 − 1
        self.assertAlmostEqual(rec["upperShadowPct"], 72.7, 1)    # (110 − 102) / (110 − 99)


class HoldersTests(unittest.TestCase):
    """P4 集保股權分散表。"""

    def test_parse_csv_encodings_and_levels(self):
        from twradar.providers.tdcc import decode, parse_csv
        text = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n20261113,2330,1,10,999,0.10\n20261113,2330,16,0,0,0\n20261113,2330,17,10,999,100\n20261113,1101,1,5,5,1\n"
        for raw in (text.encode("utf-8-sig"), text.encode("cp950")):
            rows = parse_csv(decode(raw), {"2330"})
            self.assertEqual([r["level"] for r in rows], [1, 17])
            self.assertEqual(rows[0]["date"], "20261113")

    def test_levels(self):
        from twradar.analysis import retail_levels, whale_level
        P = {**DEFAULT_RULES, "whaleAmount": 0}
        self.assertEqual(whale_level(P, {}, 2340), (15, 1000))                 # 固定千張
        self.assertEqual(whale_level(P, {"whaleLots": 400}, 2340), (12, 400))  # 個股另設
        P["whaleAmount"] = 10000                                                # 1 億元
        self.assertEqual(whale_level(P, {}, 2340), (8, 40))                    # 約 43 張 → 40 張級距
        self.assertEqual(whale_level(P, {}, 71.9), (15, 1000))                 # 約 1,391 張 → 上限 1000
        self.assertEqual(whale_level(P, {"whaleLots": 800}, 71.9), (14, 800))  # 個股設定優先
        self.assertEqual(retail_levels(P), (8, 50))                            # 50 張以下 = 第 1–8 級

    def test_weekly_crawl_and_whale_signal(self):
        from twradar.analysis import indicators_many
        from twradar.jobs import Crawler
        days = weekdays(30)
        fx = FakeExchange(days)
        _, opts = start_fake(fx)
        cfg, db, st = setup_env(opts)
        cr = Crawler(db, cfg, st, transport=Fetcher(0, "test"))
        d1 = days[-6]
        fx.tdcc_date = d1
        self.assertEqual(cr.crawl_weekly(d1), d1)
        self.assertEqual({r["code"] for r in db.q("SELECT DISTINCT code FROM holders")}, {"2330", "2454", "6488"})
        self.assertIsNone(cr.crawl_weekly(days[-5]))  # 未滿 7 天不再抓
        d2 = days[-1]
        fx.tdcc_date, fx.tdcc_whale, fx.tdcc_retail = d2, 78.5, 1300
        self.assertEqual(cr.crawl_weekly(d2), d2)
        rec = indicators_many(db, ["2330"], d2, {"whaleAmount": 0})["2330"]
        self.assertEqual((rec["holdersDate"], rec["whaleLots"], rec["whalePct"], rec["whaleChg"]), (d2, 1000, 78.5, -1.5))
        self.assertEqual(rec["holdersChgPct"], 12.5)  # 2,400 → 2,700 人
        self.assertEqual(rec["retailChg"], 300)
        # 持有面：獲利達標＋大戶減碼＋散戶增加 → 獲利保護減碼
        rec.update(close=150.0, ma20=140.0, vol=100, vol5=100, chgPct=1)
        h = evaluate(rec, {"holding": True, "cost": 100}, DEFAULT_RULES)["hold"]
        self.assertEqual((h["light"], h["rule"]), ("reduce", "獲利保護"))
        self.assertTrue(any("大戶" in x for x in h["reasons"]) and any("股東人數" in x for x in h["reasons"]))
        # 集保資料還沒更新：不重複寫入、不報錯
        fx.tdcc_date = d2
        from datetime import timedelta
        d3 = (D.to_date(d2) + timedelta(days=8)).strftime("%Y%m%d")
        n = db.one("SELECT COUNT(*) AS n FROM holders")["n"]
        self.assertIsNone(cr.crawl_weekly(d3))
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM holders")["n"], n)

    def test_weekly_source_validation(self):
        from twradar.settings import SettingsError, SettingsStore
        st = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json", 50)
        s = st.get()
        s["sources"]["weekly"] = ["twse"]
        with self.assertRaises(SettingsError) as cm:
            st.save(s)
        self.assertEqual(cm.exception.errors[0]["path"], "sources.weekly[0]")


class V3RulesTests(unittest.TestCase):
    """v0.3 規則（2026-11-13 專家審閱後）。"""
    # 未持有、回測月線有守後出帶量紅 K、法人由賣轉買
    BASE = {"close": 105.0, "open": 101.0, "high": 106, "low": 100.5, "chgPct": 3.0, "candle": 1, "vol": 150, "vol5": 100,
            "ma5": 102.0, "ma20": 100.0, "ma20Prev": 99.8, "prevHigh": 110.0, "pbLowMinPct": 0.5, "pbCloseMinPct": -0.8,
            "foreign": 300, "trust": 20, "instNet": 320, "foreignStreak": 1, "trustStreak": 1, "instTurn": True,
            "foreign5d": 100, "trust5d": 0, "marginChg": 50, "amt5": 50000, "upperShadowPct": 10, "prevFromHigh20": -5}

    def ev(self, rec=None, **stock):
        return evaluate({**self.BASE, **(rec or {})}, stock, DEFAULT_RULES)

    def test_first_entry(self):
        e = self.ev()
        self.assertEqual((e["light"], e["lightLabel"], e["rule"]), ("go", "上車訊號", "上車"))
        # 月線下彎（空頭反彈第一天）→ 不上車
        e = self.ev({"ma20Prev": 100.5, "ma5": 99})
        self.assertEqual(e["light"], "wait")
        self.assertTrue(any("月線止跌翻揚" in n for n in e["needs"]))
        # 回測期間曾有效跌破月線 → 沒守住
        self.assertEqual(self.ev({"pbCloseMinPct": -3})["light"], "wait")
        # 黑 K 不上車
        self.assertEqual(self.ev({"candle": -1, "open": 106})["light"], "wait")

    def test_add_requires_profit_and_breakout(self):
        brk = {"close": 112.0, "high": 113, "foreignStreak": 3, "instTurn": False}
        e = self.ev(brk, holding=True, cost=100)
        self.assertEqual((e["light"], e["lightLabel"]), ("go", "加碼訊號"))
        # 底倉沒賺 3% → 不加碼（不攤平），還差裡要寫出原因
        e = self.ev(brk, holding=True, cost=110)
        self.assertEqual(e["light"], "wait")
        self.assertTrue(any("不攤平" in n for n in e["needs"]))
        # 沒填成本：資料不足以判斷加碼
        self.assertEqual(self.ev(brk, holding=True)["light"], "wait")
        # 持有但沒突破前高 → 不加碼（上車條件不適用於持有中）
        self.assertEqual(self.ev(holding=True, cost=90)["light"], "wait")

    def test_margin_rules(self):
        # 融資增加 < 法人買超 30%：健康；融資 > 法人買超：追價過熱 → 否決
        self.assertEqual(self.ev()["checks"]["retail"], "pass")
        e = self.ev({"marginChg": 400})
        self.assertEqual(e["light"], "stop")
        self.assertIn("融資追價", e["warnings"][0])
        # 收黑、法人賣、融資增 → 否決
        e = self.ev({"candle": -1, "open": 107, "foreign": -100, "trust": 0, "instNet": -100, "chgPct": -1})
        self.assertEqual(e["light"], "stop")

    def test_conflicts_asymmetric(self):
        # 外資買、投信賣（投信賣 ≥ 25% 且金額 ≥ 5 日均額 3%）→ 否決
        e = self.ev({"foreign": 400, "trust": -150, "instNet": 250})
        self.assertEqual(e["light"], "stop")
        self.assertIn("投信賣、外資買", e["warnings"][0])
        # 投信買、外資賣 → 觀察（可能是投信認養）
        e = self.ev({"foreign": -150, "trust": 400, "instNet": 250})
        self.assertEqual((e["light"], e["rule"]), ("wait", "投信買、外資賣"))
        # 金額太小（< 5 日均成交金額 3%）不算對作
        self.assertEqual(self.ev({"foreign": 400, "trust": -150, "instNet": 250, "amt5": 1_000_000})["light"], "go")

    def test_amplify_v3(self):
        base = {"foreignStreak": 1, "trustStreak": 0, "instTurn": False, "trust": 0, "trust5d": 0}
        # 前 4 日淨賣、今天大買：不算放大
        self.assertEqual(self.ev({**base, "foreign": 300, "foreign5d": -500})["checks"]["inst"], "fail")
        # 前 4 日淨買 100（平均 25）、今天 300 張 × 105 元 = 3,150 萬 → 放大
        self.assertEqual(self.ev({**base, "foreign": 300, "foreign5d": 400})["checks"]["inst"], "pass")
        # 小量雜訊：今天 16 張（佔量 < 10%、金額 < 3,000 萬）→ 不算
        self.assertEqual(self.ev({**base, "foreign": 16, "foreign5d": 46, "vol": 1000})["checks"]["inst"], "fail")

    def test_market_filter(self):
        e = self.ev({"mktClose": 20000, "mktMa20": 21000, "mktMa20Prev": 21100})
        self.assertEqual((e["light"], e["rule"]), ("wait", "大盤逆風"))
        self.assertTrue(any("大盤逆風" in w for w in e["warnings"]))
        self.assertEqual(self.ev({"mktClose": 20000, "mktMa20": 21000, "mktMa20Prev": 20900})["light"], "go")

    def test_ex_rights_adjustment(self):
        from twradar.analysis import _indicators, adjust_prices
        days = sorted(D.weekdays_back("20261111", 25), reverse=True)
        # 前 24 天收 100；今天除息 5 元（參考價 95），收 96、漲 +1
        q = [{"date": d, "open": 100, "high": 101, "low": 99, "close": 100.0, "change": 0.0, "volume": 1000, "value": 1e8}
             for d in days]
        q[0].update(open=95, high=97, low=95, close=96.0, change=1.0)
        adj, ev = adjust_prices(q)
        self.assertEqual(ev, [days[0]])
        self.assertAlmostEqual(adj[1]["close"], 95.0)
        rec = _indicators(q, [], None, None, days[0])
        self.assertAlmostEqual(rec["ma20"], 95.05, 2)        # 未還原會是 99.8
        self.assertEqual(rec["prevHigh"], 95.0)
        # 中間缺了一個交易日（資料缺漏）：不調整
        trading = sorted(days)
        q2 = [q[0]] + q[2:]
        self.assertEqual(adjust_prices(q2, trading)[1], [])

    def test_upgrade_old_defaults(self):
        from twradar.rules import default_playbooks, p3_playbooks
        from twradar.settings import SettingsStore
        path = Path(tempfile.mkdtemp()) / "settings.json"
        st = SettingsStore(path, 50)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("rulesVersion")
        data["playbooks"] = p3_playbooks()
        for p_ in data["params"]:
            if p_["id"] == "profitTargetPct":
                p_["value"] = 20      # 舊預設 → 升級為 10
            if p_["id"] == "limitNearPct":
                p_["value"] = 8.5     # 使用者自己改過 → 不動
        path.write_text(json.dumps(data), encoding="utf-8")
        s = SettingsStore(path, 50).get()
        self.assertEqual(s["playbooks"], default_playbooks())
        vals = {p_["id"]: p_["value"] for p_ in s["params"]}
        self.assertEqual((vals["profitTargetPct"], vals["limitNearPct"], vals["whaleAmount"]), (10, 8.5, 10000))
        # 使用者改過的規則不覆蓋
        data["playbooks"]["entry"].insert(1, {"light": "noAdd", "label": "自訂", "when": "blowoffBlack"})
        path.write_text(json.dumps(data), encoding="utf-8")
        s = SettingsStore(path, 50).get()
        self.assertEqual(s["playbooks"]["entry"][1]["label"], "自訂")
        self.assertEqual(s["rulesVersion"], 3)

    def test_per_rule_confirm(self):
        from twradar.analysis import _apply_confirm
        prev = [{"light": "wait", "rawLight": "wait", "hold": {"light": "keep", "rawLight": "keep"}}]
        sig = {"light": "go", "lightLabel": "上車訊號", "confirm": None,
               "hold": {"light": "exit", "lightLabel": "退場", "confirm": 1}}
        _apply_confirm(sig, prev, 2)
        self.assertEqual(sig["light"], "wait")          # 上車：等第 2 天確認
        self.assertEqual(sig["pending"], "go")
        self.assertEqual(sig["hold"]["light"], "exit")  # 停損：當日執行


class BacktestTests(unittest.TestCase):
    """回溯分析：不偷看未來、股票池、交易模擬、報表。"""

    @classmethod
    def setUpClass(cls):
        from twradar.jobs import Crawler
        cls.days = weekdays(150)
        fx = FakeExchange(cls.days)
        # 2330：前 100 天在 100 附近，之後漲到 110 以上，再跌到 90 以下
        path = [100 + (i % 3) for i in range(100)] + [106 + i * 0.5 for i in range(20)] + [90 - i * 0.2 for i in range(30)]
        fx.set_path("2330", path)
        fx.taiex = {d: 20000 + i * 10 for i, d in enumerate(cls.days)}
        _, opts = start_fake(fx)
        cls.cfg, cls.db, cls.st = setup_env(opts)
        cr = Crawler(cls.db, cls.cfg, cls.st, transport=Fetcher(0, "test"))
        for d in cls.days:
            cr.crawl_day(d, final=True, analyze=False)
        cls.dates = [r["date"] for r in cls.db.q("SELECT date FROM market WHERE taiex IS NOT NULL ORDER BY date")]

    def test_no_lookahead(self):
        """回溯時的指標，必須等於當天實際用資料庫算出的指標（只用當天以前的資料）。"""
        from twradar.analysis import indicators_many
        from twradar.backtest import History
        h = History(self.db, ["2330", "6488"], self.dates[-1])
        for d in (self.dates[70], self.dates[110], self.dates[-1]):
            live = indicators_many(self.db, ["2330", "6488"], d)
            for c in ("2330", "6488"):
                bt = h.rec(c, d, DEFAULT_RULES)
                for k, v in live[c].items():
                    if k in ("source", "holdersDate", "whaleLots", "whalePct", "retailLots", "retailPeople", "holdersTotal",
                             "totalShares", "capAmt"):
                        continue
                    self.assertEqual(bt.get(k), v, (c, d, k))

    def test_universe_point_in_time(self):
        from twradar.backtest import BTConfig, rank_universe
        u = rank_universe(self.db, self.dates[100], BTConfig("", "", "", top_large=1, top_mid=2))
        self.assertEqual(u["6488"], {"上櫃"})
        self.assertEqual(sorted(x for c in ("2330", "2454") for x in u[c]), ["中型", "大型"])

    def test_trade_simulation(self):
        """自訂規則：收盤 > 105 上車、收盤 < 95 停損，檢查成交日與報酬（隔日開盤成交、扣費稅）。"""
        from twradar import backtest as B
        S = self.st.get()
        S["conditions"] = [{"id": "cUp", "label": "上", "left": {"field": "close"}, "op": ">", "right": 105},
                           {"id": "cDn", "label": "下", "left": {"field": "close"}, "op": "<", "right": 95}]
        S["playbooks"] = {"entry": [{"light": "add", "label": "上車", "when": {"all": ["notHolding", "cUp"]}},
                                    {"light": "watch", "default": True}],
                          "hold": [{"light": "exit", "label": "停損", "when": "cDn"}, {"light": "keep", "default": True}]}
        S["watchlist"] = [w for w in S["watchlist"] if w["code"] == "2330"]
        conf = B.BTConfig(start=self.dates[80], end=self.dates[-1], split=self.dates[120], universe="watchlist")
        res = B.run(self.db, S, conf)
        trades = res["results"][0]["tradeList"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (self.dates[101], self.dates[121], "停損"))
        # 買在 106（第 101 天開盤＝第 100 天收盤），賣在 90（第 121 天開盤＝第 120 天收盤）
        fee, tax = 0.001425, 0.003
        self.assertAlmostEqual(t["ret"], ((90 * (1 - fee - tax)) / (106 * (1 + fee)) - 1) * 100, 2)
        ev = {(e["kind"], e["tag"], e["regime"], e["sample"]): e for e in res["results"][0]["events"]}
        self.assertEqual(ev[("退場：停損", "全部", "全部", "全期")]["f5"]["n"], 1)

    def test_cli_and_report(self):
        import contextlib
        from types import SimpleNamespace
        from twradar.cli import run_backtest
        from twradar.server import latest_backtest
        out = Path(self.cfg.home) / "backtest" / "20261114-010000"
        os.environ["TWRADAR_HOME"] = self.cfg.home
        try:
            args = SimpleNamespace(start=None, end=None, split=None, universe="auto", grid=["profitTargetPct=8,12"],
                                   max_adds=2, out=str(out))
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = run_backtest(self.cfg, args)
            summary = latest_backtest()  # 狀態 API 會附上最新一次回溯摘要
        finally:
            os.environ["TWRADAR_HOME"] = TMP
        self.assertEqual(summary["run"], "20261114-010000")
        self.assertEqual(len(summary["variants"]), 3)
        self.assertIn("全期", summary["trades"])
        self.assertEqual(code, 0, buf.getvalue())
        html_ = (out / "report.html").read_text(encoding="utf-8")
        for k in ("回溯分析", "進場訊號", "參數組合比較", "profitTargetPct=8", "注意事項"):
            self.assertIn(k, html_)
        res = json.loads((out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(len(res["results"]), 3)
        self.assertTrue((out / "trades.csv").exists())

    def test_grid_validation(self):
        from twradar import backtest as B
        self.assertEqual(len(B.parse_grid(["a=1,2", "b=3,4"])), 5)
        with self.assertRaises(ValueError):
            B.parse_grid(["a=" + ",".join(map(str, range(61)))])
        conf = B.BTConfig(start=self.dates[80], end=self.dates[-1], split=self.dates[120],
                          variants=[("x", {"pullbackDays": 5})])
        with self.assertRaises(ValueError):
            B.run(self.db, self.st.get(), conf)

    def test_not_enough_data(self):
        import contextlib
        from types import SimpleNamespace
        from twradar.cli import run_backtest
        cfg, _, _ = setup_env({})
        os.environ["TWRADAR_HOME"] = cfg.home
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = run_backtest(cfg, SimpleNamespace(start=None, end=None, split=None, universe="auto", grid=[],
                                                         max_adds=2, out=None))
        finally:
            os.environ["TWRADAR_HOME"] = TMP
        self.assertEqual(code, 1)
        self.assertIn("backfill", buf.getvalue())


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
