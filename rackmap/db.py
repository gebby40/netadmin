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
    """
    -- Equipment catalogue (loaded from the bundled file; safe to rebuild at any time)
    CREATE TABLE device_types (
        slug TEXT PRIMARY KEY,
        manufacturer TEXT NOT NULL,
        model TEXT NOT NULL,
        part_number TEXT,
        u_height REAL NOT NULL,
        full_depth INTEGER NOT NULL,
        front_image TEXT,
        rear_image TEXT,
        ports TEXT NOT NULL,
        search TEXT NOT NULL
    );
    CREATE INDEX device_types_maker ON device_types (manufacturer, model);

    CREATE TABLE racks (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        height_u INTEGER NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- A device keeps its own copy of maker/model/height so it survives catalogue changes
    -- and so equipment that is not in the catalogue can be recorded.
    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        type_slug TEXT,
        manufacturer TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        u_height REAL NOT NULL DEFAULT 1,
        full_depth INTEGER NOT NULL DEFAULT 1,
        rack_id INTEGER REFERENCES racks(id) ON DELETE SET NULL,
        position_u REAL,
        face TEXT NOT NULL DEFAULT 'front',
        ip TEXT NOT NULL DEFAULT '',
        mac TEXT NOT NULL DEFAULT '',
        serial TEXT NOT NULL DEFAULT '',
        admin_url TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX devices_rack ON devices (rack_id);
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


def execute_many(sql: str, rows: list[tuple], before: str | None = None) -> None:
    """Run one statement for many rows in a single transaction (optionally after `before`)."""
    with _lock_for(connect()):
        conn = connect()
        conn.execute("BEGIN")
        try:
            if before:
                conn.execute(before)
            conn.executemany(sql, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_setting(key: str) -> str | None:
    rows = query("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


_write_lock = threading.Lock()


def _lock_for(_conn: sqlite3.Connection) -> threading.Lock:
    # One connection shared across threads: serialise every statement.
    return _write_lock
