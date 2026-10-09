"""Authentication: scrypt password hashes, server-side sessions (only a hash
of the session id is stored), CSRF tokens, login rate limiting and bearer
API tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from ..db import Database, now_iso

SESSION_COOKIE = "aegis_session"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, dk = encoded.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db: Database, settings):
        self.db = db
        self.settings = settings

    # -- login --------------------------------------------------------------
    def locked_out(self, client: str) -> bool:
        since = now_iso(timedelta(minutes=-LOCKOUT_MINUTES))
        fails = self.db.scalar("SELECT COUNT(*) FROM login_attempts WHERE client = ? AND success = 0 AND created_at >= ?",
                               (client, since))
        return int(fails or 0) >= MAX_FAILED_LOGINS

    def login(self, username: str, password: str, client: str) -> tuple[str, str] | None:
        if self.locked_out(client):
            raise HTTPException(429, "too many failed logins; try again later")
        ok = bool(self.settings.admin_password_hash) and hmac.compare_digest(
            username.encode(), self.settings.admin_username.encode()) and verify_password(
            password, self.settings.admin_password_hash)
        self.db.insert("login_attempts", {"client": client, "success": int(ok), "created_at": now_iso()})
        if not ok:
            return None
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.db.insert("sessions", {"id_hash": hash_token(sid), "username": username, "csrf_token": csrf,
                                    "created_at": now_iso(),
                                    "expires_at": now_iso(timedelta(hours=self.settings.session_ttl_hours))})
        return sid, csrf

    def logout(self, sid: str | None) -> None:
        if sid:
            self.db.execute("DELETE FROM sessions WHERE id_hash = ?", (hash_token(sid),))

    def session(self, request: Request) -> dict | None:
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        return self.db.one("SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?", (hash_token(sid), now_iso()))

    def bearer(self, request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer ") or not self.settings.api_token_hash:
            return None
        if hmac.compare_digest(hash_token(h[7:].strip()), self.settings.api_token_hash):
            return "api-token"
        return None

    # -- dependencies -------------------------------------------------------
    def require(self, request: Request) -> str:
        """Return the authenticated principal or raise 401. State-changing
        cookie-authenticated requests must carry a valid CSRF token."""
        principal = self.bearer(request)
        if principal:
            return principal
        sess = self.session(request)
        if not sess:
            raise HTTPException(401, "authentication required")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.headers.get("x-csrf-token") or getattr(request.state, "form_csrf", None)
            if not token or not hmac.compare_digest(token, sess["csrf_token"]):
                raise HTTPException(403, "missing or invalid CSRF token")
        request.state.csrf = sess["csrf_token"]
        return f"user:{sess['username']}"
