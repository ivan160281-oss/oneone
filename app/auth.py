"""Passwords, sessions and login throttling."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

SESSION_COOKIE = "altgeo_session"
SESSION_TTL = 30 * 24 * 3600

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_password() -> str:
    # Readable for dictating over the phone: no 0/O, 1/l/I.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def new_sync_token() -> str:
    # Goes into sync_config.txt as sync_password=; must stay free of '"' and
    # newlines since the firmware puts it into a header verbatim.
    return secrets.token_hex(12)


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash(token), user_id, now + SESSION_TTL),
    )
    conn.commit()
    return token


def session_user(conn, token: str | None):
    if not token:
        return None
    return conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
        (token_hash(token), int(time.time())),
    ).fetchone()


def drop_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        conn.commit()


class LoginThrottle:
    """At most `limit` failed logins per (login, ip) in `window` seconds."""

    def __init__(self, limit: int = 10, window: int = 900):
        self.limit, self.window = limit, window
        self.fails: dict[tuple, deque] = defaultdict(deque)

    def _trim(self, key):
        q = self.fails[key]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, login: str, ip: str) -> bool:
        return len(self._trim((login.lower(), ip))) >= self.limit

    def fail(self, login: str, ip: str) -> None:
        self._trim((login.lower(), ip)).append(time.time())

    def reset(self, login: str, ip: str) -> None:
        self.fails.pop((login.lower(), ip), None)
