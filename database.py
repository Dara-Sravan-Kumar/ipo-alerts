"""
database.py
===========
Lightweight SQLite-backed state store used purely for de-duplication:
we never want to spam the Discord channel with the same IPO twice.

The dedup key is stable per (symbol, expected_date, status) so that a
genuinely new event (e.g. an IPO moving from "expected" to "priced")
can legitimately trigger a fresh notification, while a re-run on the
same day for the same event stays silent.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_ipos (
    dedup_key   TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT,
    source      TEXT,
    sent_at     TEXT NOT NULL
);
"""


class StateStore:
    """Tracks which IPO notifications have already been delivered."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        log.debug("State store ready at %s", self.db_path)

    def already_sent(self, dedup_key: str) -> bool:
        """Return True if we've already notified for this exact event."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_ipos WHERE dedup_key = ? LIMIT 1",
                (dedup_key,),
            ).fetchone()
        return row is not None

    def mark_sent(
        self,
        dedup_key: str,
        *,
        symbol: str,
        name: str,
        status: str,
        source: str,
    ) -> None:
        """Record that a notification was delivered for this event."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_ipos
                    (dedup_key, symbol, name, status, source, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dedup_key,
                    symbol,
                    name,
                    status,
                    source,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        log.debug("Marked %s as sent", dedup_key)

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM sent_ipos").fetchone()[0]
