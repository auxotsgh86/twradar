"""系統設定：存放在資料夾 TWRADAR_HOME（預設 ~/twradar-data）的 config.json。

只放網頁不能改的系統設定（位址、token、管理密碼雜湊等）。使用者設定（觀察清單、規則、資料源、
排程、顏色）在 settings.json，見 settings.py。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW = timezone(timedelta(hours=8), "Asia/Taipei")  # 台灣沒有日光節約時間，固定 UTC+8

# v0.1.x 放在 config.json、v0.2 起移到 settings.json 的欄位（只在升級時讀取一次）
LEGACY_KEYS = ("schedule", "sources", "finmind_token", "use_finmind_fallback")


def now_tw() -> datetime:
    return datetime.now(TW)


def home_dir() -> Path:
    p = Path(os.environ.get("TWRADAR_HOME", Path.home() / "twradar-data")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pw_alphabet() -> str:
    return "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉容易看錯的 i l o 0 1


def new_password() -> str:
    """好讀的隨機密碼，例如 k7mq-3xbe-np4t（約 60 bits）。"""
    a = _pw_alphabet()
    return "-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(3))


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    # 權限：唯讀 token（晨報用）、管理 token（管理腳本用）、管理密碼（網頁登入用）
    read_token: str = ""
    admin_token: str = ""
    admin_password_hash: str = ""
    admin_initial_password: str = ""   # 安裝時產生的密碼；使用者自訂密碼後清空
    session_secret: str = ""           # 改密碼時重產，讓所有登入失效
    # 對同一個網站兩次請求之間至少間隔幾秒（避免被封鎖）
    request_interval: float = 8.0
    # 第一次啟動時往回補抓幾個交易日（算季線至少需要 60 個）
    backfill_days: int = 100
    user_agent: str = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 twradar/0.2")
    max_watchlist: int = 50
    # 以下不存檔
    legacy: dict = field(default_factory=dict, repr=False)   # 尚未搬到 settings.json 的舊欄位
    env: dict = field(default_factory=dict, repr=False)      # 環境變數覆寫（Docker），例如 finmind_token

    @property
    def token(self) -> str:
        """相容 v0.1：舊程式與文件中的 token 指唯讀 token。"""
        return self.read_token

    @property
    def db_path(self) -> Path:
        return home_dir() / "twradar.sqlite3"

    @property
    def log_path(self) -> Path:
        return home_dir() / "twradar.log"

    @property
    def settings_path(self) -> Path:
        return home_dir() / "settings.json"


_SAVED = [f.name for f in fields(Config) if f.name not in ("legacy", "env")]


def config_path() -> Path:
    return home_dir() / "config.json"


def ensure_credentials(cfg: Config) -> bool:
    """補齊缺少的 token、密碼、session 金鑰。回傳是否有變更。"""
    from .auth import hash_password  # 避免循環匯入
    changed = False
    if not cfg.read_token:
        cfg.read_token = secrets.token_urlsafe(24); changed = True
    if not cfg.admin_token:
        cfg.admin_token = secrets.token_urlsafe(32); changed = True
    if not cfg.session_secret:
        cfg.session_secret = secrets.token_hex(32); changed = True
    if not cfg.admin_password_hash:
        cfg.admin_initial_password = new_password()
        cfg.admin_password_hash = hash_password(cfg.admin_initial_password)
        changed = True
    return changed


def load_config() -> Config:
    path = config_path()
    cfg = Config()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for k, v in data.items():
        if k in _SAVED:
            setattr(cfg, k, v)
    if data.get("token") and not cfg.read_token:  # v0.1 升級：原本的 token 變成唯讀 token，晨報不用改
        cfg.read_token = data["token"]
    cfg.legacy = {k: data[k] for k in LEGACY_KEYS if k in data}
    # 環境變數可覆寫（Docker 用）
    env_map = {"host": "TWRADAR_HOST", "read_token": "TWRADAR_TOKEN", "admin_token": "TWRADAR_ADMIN_TOKEN"}
    for key, env in env_map.items():
        if os.environ.get(env):
            setattr(cfg, key, os.environ[env])
    if os.environ.get("TWRADAR_PORT"):
        cfg.port = int(os.environ["TWRADAR_PORT"])
    if os.environ.get("TWRADAR_FINMIND_TOKEN"):
        cfg.env["finmind_token"] = os.environ["TWRADAR_FINMIND_TOKEN"]
    if ensure_credentials(cfg) or not path.exists() or "token" in data:
        save_config(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    data = {k: v for k, v in asdict(cfg).items() if k in _SAVED}
    data.update(cfg.legacy)  # 尚未搬到 settings.json 前保留，避免遺失
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # 內含 token 與密碼雜湊
    except OSError:
        pass
    os.replace(tmp, config_path())
