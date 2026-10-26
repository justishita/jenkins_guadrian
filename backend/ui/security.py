"""Passwords, session cookies and CSRF for the web UI.

Passwords are bcrypt-hashed. A session is a signed JWT in an HttpOnly, SameSite=Lax cookie carrying the
user id, a per-session CSRF token and the user's ``session_version`` (so changing a password or
deactivating a user invalidates every session at once). State-changing requests must echo the CSRF
token in the ``X-CSRF-Token`` header; the cookie alone is never enough.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import re
import secrets
import threading
import time
from typing import Any

import bcrypt
import jwt

from backend.config import settings

ALGORITHM = "HS256"
MIN_PASSWORD_LENGTH = 10
MIN_SECRET_LENGTH = 32
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")

# Verified for unknown emails so a login attempt takes the same time whether or not the account exists.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12)).decode()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL.match(email))


def password_problem(password: str, email: str = "") -> str | None:
    """Why this password is unacceptable, or ``None``."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode("utf-8")) > 72:
        return "Password must be at most 72 bytes."  # bcrypt's limit; refuse instead of silently truncating
    if email and password.lower() == email.lower():
        return "Password must not be the same as the email."
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=settings.UI_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-work check; a missing hash still costs one bcrypt verification."""
    candidate = (password_hash or _DUMMY_HASH).encode("utf-8")
    try:
        matched = bcrypt.checkpw(password.encode("utf-8")[:72], candidate)
    except ValueError:
        return False
    return matched and password_hash is not None


def secret_configured() -> bool:
    return len(settings.UI_SESSION_SECRET) >= MIN_SECRET_LENGTH


@dataclass(frozen=True)
class Session:
    user_id: int
    csrf: str
    version: int


def issue_session(user_id: int, version: int, *, now: datetime | None = None) -> tuple[str, str]:
    """Return ``(cookie value, csrf token)``."""
    if not secret_configured():
        raise RuntimeError("UI_SESSION_SECRET must be at least 32 characters")
    issued = now or datetime.now(timezone.utc)
    csrf = secrets.token_urlsafe(32)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "csrf": csrf,
        "ver": version,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(hours=settings.UI_SESSION_HOURS)).timestamp()),
    }
    return jwt.encode(claims, settings.UI_SESSION_SECRET, algorithm=ALGORITHM), csrf


def read_session(token: str | None) -> Session | None:
    if not token or not secret_configured():
        return None
    try:
        claims = jwt.decode(token, settings.UI_SESSION_SECRET, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "csrf", "ver"]})
        return Session(user_id=int(claims["sub"]), csrf=str(claims["csrf"]), version=int(claims["ver"]))
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


def csrf_matches(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(expected, supplied or "")


class LoginLimiter:
    """Sliding-window failure counter keyed by (ip, email) and by email alone.

    In-memory and per process: it blunts online guessing, it is not a distributed lockout.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str]:
        return f"pair:{ip}|{email}", f"email:{email}"

    def _prune(self, key: str, now: float) -> deque[float]:
        window = settings.UI_LOGIN_WINDOW_SECONDS
        events = self._failures[key]
        while events and now - events[0] > window:
            events.popleft()
        return events

    def blocked(self, ip: str, email: str, *, now: float | None = None) -> bool:
        moment = now if now is not None else time.monotonic()
        pair, by_email = self._keys(ip, email)
        with self._lock:
            return (
                len(self._prune(pair, moment)) >= settings.UI_LOGIN_MAX_ATTEMPTS
                or len(self._prune(by_email, moment)) >= settings.UI_LOGIN_MAX_ATTEMPTS * 3
            )

    def record_failure(self, ip: str, email: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        with self._lock:
            for key in self._keys(ip, email):
                self._failures[key].append(moment)

    def reset(self, ip: str, email: str) -> None:
        with self._lock:
            self._failures.pop(self._keys(ip, email)[0], None)


limiter = LoginLimiter()
