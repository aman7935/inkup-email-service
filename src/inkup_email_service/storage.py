"""Table of delivered orders waiting for their review email.

One row per order. A row is ``pending`` until its ``due_at`` passes and the
email is sent (``sent``), turns out to be unsendable (``skipped``), or keeps
failing (``failed``).

* ``DATABASE_URL`` set (production, e.g. Neon Postgres): rows live in Postgres,
  so they survive restarts on hosts without a disk (Render free plan).
* Otherwise: a local SQLite file at ``DATABASE_PATH`` (development, tests).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .config import get_settings

_COLUMNS = "key, payload, delivered_at, due_at, status, attempts, last_error"

_SQLITE_SCHEMA = """
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

_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS reviews (
        key          TEXT PRIMARY KEY,
        payload      TEXT NOT NULL,
        delivered_at TEXT,
        due_at       DOUBLE PRECISION NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending',
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        email_id     TEXT,
        created_at   DOUBLE PRECISION NOT NULL,
        updated_at   DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS reviews_pending_due ON reviews (status, due_at)",
)


@dataclass(frozen=True, slots=True)
class ReviewRow:
    key: str
    payload: str
    delivered_at: str | None
    due_at: float
    status: str
    attempts: int
    last_error: str | None


class _Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, sql: str, params: Sequence[Any] = ...) -> Any: ...
    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class ReviewStore:
    """Same API for SQLite and Postgres. SQL is written with ``%s`` placeholders."""

    def __init__(self, path: str | Path | None = None, *, url: str | None = None):
        self._pool: Any = None
        if url:
            from psycopg_pool import ConnectionPool

            # Neon closes idle connections when the database scales to zero;
            # `check` tests each connection before handing it out.
            self._pool = ConnectionPool(
                url,
                min_size=1,
                max_size=4,
                check=ConnectionPool.check_connection,
                # Neon's "-pooler" endpoint is PgBouncer: no server-side prepared statements.
                kwargs={"autocommit": False, "prepare_threshold": None},
                open=True,
            )
            self.backend = "postgres"
            with self._cursor() as cur:
                for statement in _POSTGRES_SCHEMA:
                    cur.execute(statement)
        else:
            self.path = str(path or "data/inkup.db")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.backend = "sqlite"
            db = sqlite3.connect(self.path, timeout=10)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SQLITE_SCHEMA)
            finally:
                db.close()

    @contextmanager
    def _cursor(self) -> Iterator[_Cursor]:
        """One transaction: committed on success, rolled back on error."""
        if self._pool is not None:
            with self._pool.connection() as conn, conn.cursor() as cur:
                yield cur
            return
        # A short-lived connection per call is safe across the web threadpool
        # and the background sender.
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield _SqliteCursor(db.cursor())
        finally:
            db.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    # ------------------------------------------------------------------
    def add(self, key: str, payload: str, *, delivered_at: str | None, due_at: float) -> bool:
        """Insert a new pending row. Returns False if the order is already known."""
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO reviews (key, payload, delivered_at, due_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                (key, payload, delivered_at, due_at, now, now),
            )
            return cur.rowcount == 1

    def due(self, *, now: float, limit: int) -> list[ReviewRow]:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM reviews WHERE status = 'pending' AND due_at <= %s "
                "ORDER BY due_at LIMIT %s",
                (now, limit),
            )
            rows = cur.fetchall()
        return [ReviewRow(*row) for row in rows]

    def get(self, key: str) -> ReviewRow | None:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM reviews WHERE key = %s", (key,))
            row = cur.fetchone()
        return ReviewRow(*row) if row else None

    def mark_sent(self, key: str, email_id: str | None) -> None:
        self._finish(key, "sent", None, email_id)

    def mark_skipped(self, key: str, reason: str) -> None:
        self._finish(key, "skipped", reason, None)

    def mark_failed(self, key: str, reason: str) -> None:
        self._finish(key, "failed", reason, None)

    def retry_later(self, key: str, *, error: str, next_due: float) -> int:
        """Record a failed attempt and push the row back. Returns the attempt count."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE reviews SET attempts = attempts + 1, last_error = %s, due_at = %s, "
                "updated_at = %s WHERE key = %s",
                (error[:500], next_due, time.time(), key),
            )
            cur.execute("SELECT attempts FROM reviews WHERE key = %s", (key,))
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def counts(self) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM reviews GROUP BY status")
            rows = cur.fetchall()
        return {status: int(count) for status, count in rows}

    def _finish(self, key: str, status: str, reason: str | None, email_id: str | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE reviews SET status = %s, last_error = %s, email_id = %s, updated_at = %s "
                "WHERE key = %s",
                (status, reason[:500] if reason else None, email_id, time.time(), key),
            )


class _SqliteCursor:
    """Lets SQLite run the same ``%s``-style SQL as Postgres."""

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._cur.execute(sql.replace("%s", "?"), params)

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()


@lru_cache(maxsize=1)
def get_store() -> ReviewStore:
    settings = get_settings()
    url = settings.database_url.get_secret_value() if settings.database_url else None
    return ReviewStore(settings.database_path, url=url)
