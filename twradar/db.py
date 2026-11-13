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
