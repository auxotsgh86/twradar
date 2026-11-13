"""權限：兩種角色（reader 唯讀、admin 管理者）。

驗證透過 Authenticator 介面，日後要接外部權限系統時換掉實作即可，API 不必改。

預設實作 TokenPasswordAuthenticator：
- 唯讀 token：Authorization: Bearer / X-Token / ?token=（晨報等只能發 GET 的工具）
- 管理 token：只接受標頭（避免出現在網址與紀錄裡）
- 管理密碼：網頁登入，發放簽章 cookie（HttpOnly、SameSite=Strict，30 天）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol

READER, ADMIN = "reader", "admin"
COOKIE = "twradar_session"
SESSION_DAYS = 30
PBKDF2_ITER = 200_000


@dataclass(frozen=True)
class Principal:
    role: str     # "reader" | "admin"
    via: str      # "token" | "session" | 其他實作自訂
    name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


@dataclass
class Credentials:
    """從 HTTP 請求取出的憑證，與 http.server 無關，方便替換與測試。"""
    bearer: str = ""        # Authorization: Bearer 或 X-Token
    query_token: str = ""   # ?token=
    cookie: str = ""        # session cookie 值
    client: str = ""        # 用戶端 IP


class Authenticator(Protocol):
    def authenticate(self, cred: Credentials) -> Principal | None: ...
    def login(self, password: str, client: str) -> str: ...          # 回傳 session cookie 值；失敗丟 AuthError
    def change_password(self, current: str, new: str) -> str: ...     # 回傳新的 session cookie 值
    def tokens(self) -> dict: ...
    def rotate_token(self, kind: str) -> str: ...


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


# ---------------- 密碼雜湊 ----------------

def hash_password(password: str, iterations: int = PBKDF2_ITER) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# ---------------- 登入失敗鎖定 ----------------

class Throttle:
    """同一 IP 15 分鐘內失敗 5 次、或全部來源失敗 20 次，鎖定 15 分鐘。"""

    def __init__(self, per_client: int = 5, global_limit: int = 20, window: float = 900, lock: float = 900):
        self.per_client, self.global_limit, self.window, self.lock_for = per_client, global_limit, window, lock
        self.fails: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        xs = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = xs
        return xs

    def check(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in (client, "*"):
                until = self.locked_until.get(key, 0)
                if until > now:
                    raise AuthError(f"登入失敗太多次，請 {int((until - now) // 60) + 1} 分鐘後再試", 429)

    def fail(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key, limit in ((client, self.per_client), ("*", self.global_limit)):
                xs = self._recent(key, now)
                xs.append(now)
                if len(xs) >= limit:
                    self.locked_until[key] = now + self.lock_for
                    self.fails[key] = []

    def ok(self, client: str) -> None:
        with self._lock:
            self.fails.pop(client, None)


# ---------------- 預設實作 ----------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenPasswordAuthenticator:
    def __init__(self, cfg, save=None, clock=time.time):
        """cfg：Config；save：變更後寫回設定檔的函式（預設 config.save_config）。"""
        from .config import save_config
        self.cfg = cfg
        self.save = save or save_config
        self.clock = clock
        self.throttle = Throttle()
        self._lock = threading.Lock()

    # ---- 驗證 ----
    def authenticate(self, cred: Credentials) -> Principal | None:
        """同時帶多種憑證時取權限最高者（例如網址帶唯讀 token、又已用密碼登入）。"""
        c = self.cfg
        found: list[Principal] = []
        if cred.bearer:
            if c.admin_token and hmac.compare_digest(cred.bearer, c.admin_token):
                return Principal(ADMIN, "token")
            if c.read_token and hmac.compare_digest(cred.bearer, c.read_token):
                found.append(Principal(READER, "token"))
        if cred.cookie:
            p = self._check_session(cred.cookie)
            if p:
                return p
        if cred.query_token:
            if c.admin_token and hmac.compare_digest(cred.query_token, c.admin_token):
                raise AuthError("管理 token 請放在 Authorization 標頭，不要放在網址", 400)
            if c.read_token and hmac.compare_digest(cred.query_token, c.read_token):
                found.append(Principal(READER, "token"))
        return found[0] if found else None

    # ---- session cookie ----
    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self.cfg.session_secret.encode(), payload, hashlib.sha256).digest())

    def issue_session(self) -> str:
        payload = json.dumps({"r": ADMIN, "exp": int(self.clock()) + SESSION_DAYS * 86400}, separators=(",", ":")).encode()
        return _b64(payload) + "." + self._sign(payload)

    def _check_session(self, value: str) -> Principal | None:
        try:
            p64, sig = value.split(".", 1)
            payload = _unb64(p64)
        except (ValueError, TypeError):
            return None
        if not self.cfg.session_secret or not hmac.compare_digest(sig, self._sign(payload)):
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if data.get("exp", 0) < self.clock() or data.get("r") != ADMIN:
            return None
        return Principal(ADMIN, "session")

    # ---- 登入／密碼 ----
    def login(self, password: str, client: str) -> str:
        self.throttle.check(client)
        if not password or not verify_password(password, self.cfg.admin_password_hash):
            self.throttle.fail(client)
            raise AuthError("密碼錯誤", 401)
        self.throttle.ok(client)
        return self.issue_session()

    def change_password(self, current: str, new: str) -> str:
        if not verify_password(current or "", self.cfg.admin_password_hash):
            raise AuthError("目前的密碼不正確", 403)
        validate_new_password(new)
        with self._lock:
            self.cfg.admin_password_hash = hash_password(new)
            self.cfg.admin_initial_password = ""
            self.cfg.session_secret = secrets.token_hex(32)  # 其他裝置的登入全部失效
            self.save(self.cfg)
        return self.issue_session()

    def change_password_local(self, new: str) -> None:
        """主機上執行（CLI）：不需要舊密碼。"""
        validate_new_password(new)
        with self._lock:
            self.cfg.admin_password_hash = hash_password(new)
            self.cfg.admin_initial_password = ""
            self.cfg.session_secret = secrets.token_hex(32)
            self.save(self.cfg)

    def reset_password(self) -> str:
        """主機上執行（CLI）：產生新的隨機密碼並回傳。"""
        from .config import new_password
        pw = new_password()
        with self._lock:
            self.cfg.admin_password_hash = hash_password(pw)
            self.cfg.admin_initial_password = pw
            self.cfg.session_secret = secrets.token_hex(32)
            self.save(self.cfg)
        return pw

    # ---- token ----
    def tokens(self) -> dict:
        return {"readToken": self.cfg.read_token, "adminToken": self.cfg.admin_token,
                "initialPassword": bool(self.cfg.admin_initial_password)}

    def rotate_token(self, kind: str) -> str:
        if kind not in ("read", "admin"):
            raise AuthError("kind 必須是 read 或 admin", 400)
        new = secrets.token_urlsafe(24 if kind == "read" else 32)
        with self._lock:
            setattr(self.cfg, f"{kind}_token", new)
            self.save(self.cfg)
        return new


def validate_new_password(pw: str) -> None:
    if not isinstance(pw, str) or len(pw) < 8:
        raise AuthError("新密碼至少 8 個字元", 400)
    if len(pw) > 200:
        raise AuthError("新密碼太長", 400)
