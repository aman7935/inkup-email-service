"""SQLite table of delivered orders waiting for their review email.

One row per order. A row is ``pending`` until its ``due_at`` passes and the
email is sent (``sent``), turns out to be unsendable (``skipped``), or keeps
failing (``failed``). SQLite needs no server; on Render it lives on a
persistent disk.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    key          TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    delivered_at TEXT,
    due_at       REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    email_id     TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_pending_due ON reviews (status, due_at);
"""


@dataclass(frozen=True, slots=True)
class ReviewRow:
    key: str
    payload: str
    delivered_at: str | None
    due_at: float
    status: str
    attempts: int
    last_error: str | None


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A short-lived connection per call keeps this safe across the web
        # threadpool and the background sender.
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, key: str, payload: str, *, delivered_at: str | None, due_at: float) -> bool:
        """Insert a new pending row. Returns False if the order is already known."""
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO reviews "
                "(key, payload, delivered_at, due_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, payload, delivered_at, due_at, now, now),
            )
            return cur.rowcount == 1

    def due(self, *, now: float, limit: int) -> list[ReviewRow]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT key, payload, delivered_at, due_at, status, attempts, last_error "
                "FROM reviews WHERE status = 'pending' AND due_at <= ? "
                "ORDER BY due_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [ReviewRow(*row) for row in rows]

    def get(self, key: str) -> ReviewRow | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT key, payload, delivered_at, due_at, status, attempts, last_error "
                "FROM reviews WHERE key = ?",
                (key,),
            ).fetchone()
        return ReviewRow(*row) if row else None

    def mark_sent(self, key: str, email_id: str | None) -> None:
        self._finish(key, "sent", None, email_id)

    def mark_skipped(self, key: str, reason: str) -> None:
        self._finish(key, "skipped", reason, None)

    def mark_failed(self, key: str, reason: str) -> None:
        self._finish(key, "failed", reason, None)

    def retry_later(self, key: str, *, error: str, next_due: float) -> int:
        """Record a failed attempt and push the row back. Returns the attempt count."""
        with self._connect() as db:
            db.execute(
                "UPDATE reviews SET attempts = attempts + 1, last_error = ?, due_at = ?, "
                "updated_at = ? WHERE key = ?",
                (error[:500], next_due, time.time(), key),
            )
            row = db.execute("SELECT attempts FROM reviews WHERE key = ?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) FROM reviews GROUP BY status").fetchall()
        return {status: count for status, count in rows}

    def _finish(self, key: str, status: str, reason: str | None, email_id: str | None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE reviews SET status = ?, last_error = ?, email_id = ?, updated_at = ? "
                "WHERE key = ?",
                (status, reason[:500] if reason else None, email_id, time.time(), key),
            )


@lru_cache(maxsize=1)
def get_store() -> ReviewStore:
    return ReviewStore(get_settings().database_path)
