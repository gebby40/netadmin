"""SQLite storage. One file in the data directory; schema changes are additive and versioned."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("RACKMAP_DATA", "./data")).resolve()

# Each entry is applied once, in order. Never edit an entry that has shipped; add a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        pw_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    """Open (once) and migrate the database."""
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(DATA_DIR, 0o700)
            conn = sqlite3.connect(DATA_DIR / "rackmap.db", check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _migrate(conn)
            _conn = conn
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version={i};COMMIT;")


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock_for(connect()):
        return connect().execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    """Run a write; returns the last inserted row id."""
    with _lock_for(connect()):
        return connect().execute(sql, params).lastrowid or 0


_write_lock = threading.Lock()


def _lock_for(_conn: sqlite3.Connection) -> threading.Lock:
    # One connection shared across threads: serialise every statement.
    return _write_lock
