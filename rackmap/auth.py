"""Accounts and sessions.

There is no built-in account: the first visitor creates the administrator. Passwords are stored as
scrypt hashes; session cookies hold a random token whose SHA-256 is what the database keeps.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = "rackmap_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10
_SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)

# Failed sign-ins per client address: {address: (count, first_failure_time)}
_failures: dict[str, tuple[int, float]] = {}
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


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
    return hmac.compare_digest(digest, bytes.fromhex(digest_hex))


def has_users() -> bool:
    return bool(db.query("SELECT 1 FROM users LIMIT 1"))


def create_user(name: str, password: str) -> int:
    return db.execute("INSERT INTO users (name, pw_hash) VALUES (?, ?)", (name, hash_password(password)))


def locked_out(client: str) -> bool:
    count, since = _failures.get(client, (0, 0.0))
    if count >= MAX_FAILURES and time.time() - since < LOCKOUT_SECONDS:
        return True
    if time.time() - since >= LOCKOUT_SECONDS:
        _failures.pop(client, None)
    return False


def check_login(name: str, password: str, client: str) -> int | None:
    """Return the user id when the name and password match, else None (and count the failure)."""
    rows = db.query("SELECT id, pw_hash FROM users WHERE name = ?", (name,))
    # Always do one hash so a wrong name takes as long as a wrong password.
    stored = rows[0]["pw_hash"] if rows else hash_password("placeholder-password")
    if verify_password(password, stored) and rows:
        _failures.pop(client, None)
        return rows[0]["id"]
    count, since = _failures.get(client, (0, time.time()))
    _failures[client] = (count + 1, since)
    return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), user_id, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    rows = db.query(
        "SELECT u.id, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > datetime('now')",
        (_hash_token(token),),
    )
    return dict(rows[0]) if rows else None


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
