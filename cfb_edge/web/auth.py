"""A single shared password, a signed cookie, and a login throttle.

This is deliberately small: one password everyone uses, checked in constant
time, exchanged for an HMAC-signed cookie so the password is not re-sent on
every request. It keeps a public URL from being world-readable. It is not a
user system and should not be treated as one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field

COOKIE_NAME = "cfb_edge_session"
DEFAULT_MAX_AGE_DAYS = 30

# Login throttle: this many failures from one address inside the window and
# further attempts are refused until it clears.
MAX_FAILURES = 8
FAILURE_WINDOW_SECONDS = 300


class AuthNotConfigured(RuntimeError):
    """No password is set, so the dashboard refuses to serve anything."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def derive_secret(password: str, secret_key: str | None = None) -> bytes:
    """Cookie signing key.

    An explicit SECRET_KEY is best. Falling back to a hash of the password
    keeps sessions valid across restarts (a random per-process key would log
    everyone out on every deploy), and changing the password invalidates every
    existing cookie, which is the behaviour you want anyway.
    """
    if secret_key:
        return hashlib.sha256(secret_key.encode("utf-8")).digest()
    return hashlib.sha256(b"cfb-edge-session:" + password.encode("utf-8")).digest()


@dataclass
class Auth:
    password: str | None
    secret_key: str | None = None
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    _failures: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Auth":
        env = os.environ if env is None else env
        password = (env.get("DASHBOARD_PASSWORD") or "").strip() or None
        return cls(password=password, secret_key=env.get("SECRET_KEY") or None)

    @property
    def configured(self) -> bool:
        return bool(self.password)

    def require_configured(self) -> None:
        if not self.configured:
            raise AuthNotConfigured(
                "DASHBOARD_PASSWORD is not set, so the dashboard has nothing to "
                "check visitors against and refuses to serve. Set it in the "
                "environment (Railway: Variables) and redeploy."
            )

    # -- password ---------------------------------------------------------

    def check_password(self, candidate: str) -> bool:
        self.require_configured()
        return hmac.compare_digest(
            (candidate or "").encode("utf-8"), self.password.encode("utf-8")
        )

    # -- throttle ---------------------------------------------------------

    def _recent_failures(self, client: str, now: float) -> list[float]:
        cutoff = now - FAILURE_WINDOW_SECONDS
        recent = [t for t in self._failures.get(client, []) if t > cutoff]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return recent

    def throttled(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent_failures(client, now)) >= MAX_FAILURES

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent_failures(client, now)
        self._failures.setdefault(client, []).append(now)

    def clear_failures(self, client: str) -> None:
        self._failures.pop(client, None)

    def seconds_until_unthrottled(self, client: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        recent = self._recent_failures(client, now)
        if len(recent) < MAX_FAILURES:
            return 0
        return max(int(FAILURE_WINDOW_SECONDS - (now - min(recent))), 1)

    # -- cookie -----------------------------------------------------------

    @property
    def max_age_seconds(self) -> int:
        return self.max_age_days * 24 * 3600

    def issue_token(self, now: float | None = None) -> str:
        self.require_configured()
        now = time.time() if now is None else now
        expires = str(int(now + self.max_age_seconds))
        signature = hmac.new(
            derive_secret(self.password, self.secret_key), expires.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{expires}.{_b64(signature)}"

    def valid_token(self, token: str | None, now: float | None = None) -> bool:
        if not token or not self.configured:
            return False
        now = time.time() if now is None else now
        expires, _, signature = token.partition(".")
        if not signature:
            return False
        try:
            if int(expires) < now:
                return False
        except ValueError:
            return False
        expected = hmac.new(
            derive_secret(self.password, self.secret_key), expires.encode("ascii"), hashlib.sha256
        ).digest()
        return hmac.compare_digest(signature, _b64(expected))
