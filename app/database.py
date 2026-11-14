"""SQLite connection management and schema.

Concurrency model
------------------
* The database runs in WAL mode, so readers never block the writer and vice
  versa. Each request opens its own connection; a query therefore sees one
  consistent snapshot (a complete, committed log version) even while an
  upload transaction is in flight.
* Writes are additionally serialised by ``write_lock`` and use
  ``BEGIN IMMEDIATE`` so that two concurrent uploads cannot interleave their
  validation/commit work.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn():
    """Connection for a single request / unit of work."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_no       TEXT PRIMARY KEY,
    event_ts       TEXT NOT NULL,
    event_us       INTEGER NOT NULL,
    parent_part_no TEXT NOT NULL,
    child_part_no  TEXT NOT NULL,
    child_batch    TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('INSTALL', 'REMOVE')),
    content_hash   TEXT NOT NULL,
    upload_batch   INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_events_order
    ON events (event_us, event_no);
CREATE INDEX IF NOT EXISTS idx_events_child
    ON events (child_part_no, child_batch);
CREATE INDEX IF NOT EXISTS idx_events_parent
    ON events (parent_part_no);

CREATE TABLE IF NOT EXISTS upload_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    accepted    INTEGER NOT NULL,
    new_events  INTEGER NOT NULL,
    duplicate_events INTEGER NOT NULL,
    detail      TEXT NOT NULL
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
