"""設定檔：存放在資料夾 TWRADAR_HOME（預設 ~/twradar-data）的 config.json。"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW = timezone(timedelta(hours=8), "Asia/Taipei")  # 台灣沒有日光節約時間，固定 UTC+8


def now_tw() -> datetime:
    return datetime.now(TW)


def home_dir() -> Path:
    p = Path(os.environ.get("TWRADAR_HOME", Path.home() / "twradar-data")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""
    # 對同一個網站兩次請求之間至少間隔幾秒（避免被封鎖）
    request_interval: float = 8.0
    # 每日排程（台北時間）。前三個抓當天，最後一個在隔天清晨補抓前一交易日缺漏
    schedule: list = field(default_factory=lambda: ["17:40", "21:50", "23:40", "06:40"])
    # 第一次啟動時往回補抓幾個交易日（算季線至少需要 60 個）
    backfill_days: int = 100
    # 選填：FinMind API token（官方來源抓不到時的備援），https://finmindtrade.com
    finmind_token: str = ""
    use_finmind_fallback: bool = True
    # 資料源：primary 依序抓全市場資料；fallback 為逐檔備援（依序嘗試）；options 以資料源 id 為鍵
    sources: dict = field(default_factory=lambda: {"primary": ["twse", "tpex"], "fallback": ["finmind"], "options": {}})
    user_agent: str = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 twradar/0.1")
    max_watchlist: int = 50

    def source_plan(self) -> tuple[list[str], list[str], dict]:
        """回傳 (primary, fallback, options)。相容舊設定 use_finmind_fallback、finmind_token。"""
        src = {"primary": ["twse", "tpex"], "fallback": ["finmind"], "options": {}, **(self.sources or {})}
        fallback = list(src["fallback"]) if self.use_finmind_fallback else []
        options = {k: dict(v) for k, v in (src.get("options") or {}).items()}
        if self.finmind_token:
            options.setdefault("finmind", {}).setdefault("token", self.finmind_token)
        return list(src["primary"]), fallback, options

    @property
    def db_path(self) -> Path:
        return home_dir() / "twradar.sqlite3"

    @property
    def log_path(self) -> Path:
        return home_dir() / "twradar.log"


def config_path() -> Path:
    return home_dir() / "config.json"


def load_config() -> Config:
    path = config_path()
    cfg = Config()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    # 環境變數可覆寫（Docker 用）
    for key in ("host", "token", "finmind_token"):
        env = os.environ.get("TWRADAR_" + key.upper())
        if env:
            setattr(cfg, key, env)
    if os.environ.get("TWRADAR_PORT"):
        cfg.port = int(os.environ["TWRADAR_PORT"])
    if not cfg.token:
        cfg.token = secrets.token_urlsafe(24)
        save_config(cfg)
    elif not path.exists():
        save_config(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    config_path().write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
