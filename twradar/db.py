"""SQLite 資料庫：結構與存取函式。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS stocks (            -- v0.1 觀察清單（v0.2 起改存 settings.json，只供遷移）
  code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'TWSE',
  grp TEXT DEFAULT '', note TEXT DEFAULT '', key_level REAL, sort_order INTEGER DEFAULT 999,
  added_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS quotes (            -- 每日收盤行情（全市場）
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, market TEXT,
  open REAL, high REAL, low REAL, close REAL, change REAL, volume INTEGER, value INTEGER,
  src TEXT, PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS inst (              -- 三大法人買賣超（股）
  date TEXT NOT NULL, code TEXT NOT NULL, foreign_net INTEGER, trust_net INTEGER,
  dealer_net INTEGER, total_net INTEGER, src TEXT, PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS margin (            -- 融資融券餘額（張）
  date TEXT NOT NULL, code TEXT NOT NULL, margin_prev INTEGER, margin_bal INTEGER,
  short_prev INTEGER, short_bal INTEGER, src TEXT, PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS market (            -- 大盤
  date TEXT PRIMARY KEY, taiex REAL, taiex_chg REAL, taiex_chg_pct REAL,
  foreign_amt INTEGER, trust_amt INTEGER, dealer_amt INTEGER, total_amt INTEGER, src TEXT
);
CREATE TABLE IF NOT EXISTS holders (           -- 集保股權分散（週）；level 1–15 為持股分級，17 為合計
  date TEXT NOT NULL, code TEXT NOT NULL, level INTEGER NOT NULL, people INTEGER, shares INTEGER, pct REAL,
  src TEXT, PRIMARY KEY (date, code, level)
);
CREATE TABLE IF NOT EXISTS runs (              -- 每個交易日的抓取狀態
  date TEXT PRIMARY KEY, status TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT, message TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS analysis (          -- 每個交易日的分析狀態
  date TEXT PRIMARY KEY, status TEXT NOT NULL, analyzed_at TEXT, rules TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS signals (           -- 每檔訊號結果
  date TEXT NOT NULL, code TEXT NOT NULL, light TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (            -- 系統紀錄（UI 顯示）
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT DEFAULT (datetime('now')), level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS track (             -- 上線後追蹤（影子組合）：每天股票池每檔的進場判定與之後的報酬
  date TEXT NOT NULL, code TEXT NOT NULL, grp TEXT NOT NULL, tags TEXT, kind TEXT NOT NULL, rule TEXT, version TEXT,
  f5 REAL, f10 REAL, f20 REAL, PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_track_kind ON track(kind, date);
CREATE TABLE IF NOT EXISTS scan_track (        -- 全市場清單上線後追蹤：流動性合格的股票池、各排序方法的名次、之後的報酬
  date TEXT NOT NULL, code TEXT NOT NULL, trend INTEGER, ranks TEXT, f5 REAL, f10 REAL, f20 REAL, PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS alerts (            -- 注意股、處置股公告（date＝公布日期）
  date TEXT NOT NULL, code TEXT NOT NULL, kind TEXT NOT NULL, market TEXT, name TEXT, count INTEGER, reason TEXT,
  start TEXT, end TEXT, measure TEXT, src TEXT, PRIMARY KEY (date, code, kind)
);
CREATE TABLE IF NOT EXISTS macro (             -- 大盤環境：外資台指期淨未平倉（口）、美元兌新台幣、市場廣度（站上月線家數%）
  date TEXT PRIMARY KEY, fut_foreign INTEGER, fut_trust INTEGER, fut_dealer INTEGER, usd_twd REAL, breadth20 REAL, src TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_code ON alerts(code, date);
CREATE INDEX IF NOT EXISTS idx_quotes_code ON quotes(code, date);
CREATE INDEX IF NOT EXISTS idx_inst_code ON inst(code, date);
CREATE INDEX IF NOT EXISTS idx_margin_code ON margin(code, date);
CREATE INDEX IF NOT EXISTS idx_holders_code ON holders(code, date);
"""

_lock = threading.RLock()


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with _lock:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with _lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with _lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with _lock:
            return self.conn.execute(sql, args).fetchone()

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    def log(self, level: str, message: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(level,message) VALUES(?,?)", (level, message))
            c.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")

    # ---------- runs ----------
    def get_run(self, date: str) -> dict | None:
        r = self.one("SELECT * FROM runs WHERE date=?", (date,))
        if not r:
            return None
        d = dict(r)
        d["steps"] = json.loads(d["steps"] or "{}")
        return d

    def save_run(self, run: dict) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO runs(date,status,steps,attempts,started_at,finished_at,message)
              VALUES(:date,:status,:steps,:attempts,:started_at,:finished_at,:message)
              ON CONFLICT(date) DO UPDATE SET status=excluded.status, steps=excluded.steps, attempts=excluded.attempts,
              started_at=excluded.started_at, finished_at=excluded.finished_at, message=excluded.message""",
                      {**run, "steps": json.dumps(run.get("steps", {}), ensure_ascii=False),
                       "message": run.get("message", "")})

    def trading_dates(self, upto: str | None = None, limit: int = 400) -> list[str]:
        """有收盤資料的日期（新到舊）。"""
        if upto:
            rows = self.q("SELECT DISTINCT date FROM quotes WHERE date<=? ORDER BY date DESC LIMIT ?", (upto, limit))
        else:
            rows = self.q("SELECT DISTINCT date FROM quotes ORDER BY date DESC LIMIT ?", (limit,))
        return [r["date"] for r in rows]

    # ---------- bulk writes ----------
    def put_quotes(self, rows: list[dict]) -> int:
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO quotes(date,code,name,market,open,high,low,close,change,volume,value,src)
              VALUES(:date,:code,:name,:market,:open,:high,:low,:close,:change,:volume,:value,:src)""",
                          [{"open": None, "high": None, "low": None, "value": None, "name": None, **r} for r in rows])
        return len(rows)

    def put_inst(self, rows: list[dict]) -> int:
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO inst(date,code,foreign_net,trust_net,dealer_net,total_net,src)
              VALUES(:date,:code,:foreign_net,:trust_net,:dealer_net,:total_net,:src)""",
                          [{"dealer_net": None, "total_net": None, **r} for r in rows])
        return len(rows)

    def put_margin(self, rows: list[dict]) -> int:
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO margin(date,code,margin_prev,margin_bal,short_prev,short_bal,src)
              VALUES(:date,:code,:margin_prev,:margin_bal,:short_prev,:short_bal,:src)""", rows)
        return len(rows)

    def put_holders(self, rows: list[dict]) -> int:
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO holders(date,code,level,people,shares,pct,src)
              VALUES(:date,:code,:level,:people,:shares,:pct,:src)""", rows)
        return len(rows)

    def put_alerts(self, rows: list[dict]) -> int:
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO alerts(date,code,kind,market,name,count,reason,start,end,measure,src)
              VALUES(:date,:code,:kind,:market,:name,:count,:reason,:start,:end,:measure,:src)""", rows)
        return len(rows)

    def alerts_for(self, codes: list[str] | None, d: str) -> dict[str, dict]:
        """d 當天每檔的狀態：{"disposal": 處置中（起迄、措施、第幾次）, "notice": 當天的注意公告, "notices10": 近 10 個交易日注意次數}。"""
        ds = [r["date"] for r in self.q("SELECT date FROM market WHERE date<=? AND taiex IS NOT NULL ORDER BY date DESC LIMIT 10", (d,))]
        since = min(ds) if ds else d
        cond, args = "", ()
        if codes is not None:
            if not codes:
                return {}
            cond, args = f" AND code IN ({','.join('?' * len(codes))})", tuple(codes)
        out: dict[str, dict] = {}
        for r in self.q(f"SELECT * FROM alerts WHERE kind='處置' AND start<=? AND end>=?{cond} ORDER BY date", (d, d, *args)):
            out.setdefault(r["code"], {})["name"] = r["name"]
            out[r["code"]]["disposal"] = {"start": r["start"], "end": r["end"], "count": r["count"],
                                                         "measure": r["measure"], "reason": r["reason"]}
        for r in self.q(f"SELECT code, COUNT(*) AS n, MAX(date) AS last FROM alerts WHERE kind='注意' AND date>=? AND date<=?{cond} "
                        "GROUP BY code", (since, d, *args)):
            out.setdefault(r["code"], {})["notices10"] = r["n"]
        for r in self.q(f"SELECT * FROM alerts WHERE kind='注意' AND date=?{cond}", (d, *args)):
            out.setdefault(r["code"], {})["notice"] = {"count": r["count"], "reason": r["reason"]}
            out[r["code"]].setdefault("name", r["name"])
        return out

    MACRO_COLS = ("fut_foreign", "fut_trust", "fut_dealer", "usd_twd", "breadth20", "src")

    def put_macro(self, rows: list[dict]) -> int:
        """合併寫入（只更新有值的欄位）。"""
        with self.tx() as c:
            for row in rows:
                cur = c.execute("SELECT * FROM macro WHERE date=?", (row["date"],)).fetchone()
                m = dict(cur) if cur else {"date": row["date"]}
                m.update({k: v for k, v in row.items() if v is not None})
                cols = ("date", *self.MACRO_COLS)
                c.execute(f"INSERT OR REPLACE INTO macro({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [m.get(k) for k in cols])
        return len(rows)

    def holders_latest(self) -> str | None:
        r = self.one("SELECT MAX(date) AS d FROM holders")
        return r["d"] if r else None

    def put_market(self, row: dict) -> None:
        with self.tx() as c:
            cur = c.execute("SELECT * FROM market WHERE date=?", (row["date"],)).fetchone()
            merged = dict(cur) if cur else {}
            merged.update({k: v for k, v in row.items() if v is not None})
            cols = ["date", "taiex", "taiex_chg", "taiex_chg_pct", "foreign_amt", "trust_amt", "dealer_amt", "total_amt", "src"]
            c.execute(f"INSERT OR REPLACE INTO market({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                      [merged.get(k) for k in cols])

    def backup_to(self, dest: str) -> None:
        with _lock:
            out = sqlite3.connect(dest)
            self.conn.backup(out)
            out.close()
