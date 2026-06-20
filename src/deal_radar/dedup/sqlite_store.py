"""SQLite-backed 'seen' store."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..models import Evaluation, Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    item_name     TEXT NOT NULL,
    listing_id    TEXT NOT NULL,
    first_seen_ts REAL NOT NULL,
    last_seen_ts  REAL NOT NULL,
    last_price    REAL,
    rating        INTEGER,
    matched       INTEGER,
    title         TEXT,
    url           TEXT,
    PRIMARY KEY (item_name, listing_id)
);
"""


class SqliteSeenStore:
    """Persists which (item, listing) pairs have been seen, so we never notify twice."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.parent and str(self._path.parent) not in ("", "."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteSeenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def is_seen(self, item_name: str, listing_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen WHERE item_name = ? AND listing_id = ?",
            (item_name, listing_id),
        )
        return cur.fetchone() is not None

    def mark_seen(
        self, item_name: str, listing: Listing, evaluation: Evaluation | None = None
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO seen
                (item_name, listing_id, first_seen_ts, last_seen_ts,
                 last_price, rating, matched, title, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_name, listing_id) DO UPDATE SET
                last_seen_ts = excluded.last_seen_ts,
                last_price   = excluded.last_price,
                rating       = COALESCE(excluded.rating, seen.rating),
                matched      = COALESCE(excluded.matched, seen.matched)
            """,
            (
                item_name,
                listing.id,
                now,
                now,
                listing.price,
                evaluation.rating if evaluation else None,
                int(evaluation.match) if evaluation else None,
                listing.title,
                listing.url,
            ),
        )
        self._conn.commit()

    def last_price(self, item_name: str, listing_id: str) -> float | None:
        cur = self._conn.execute(
            "SELECT last_price FROM seen WHERE item_name = ? AND listing_id = ?",
            (item_name, listing_id),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def list_seen(self, item_name: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT item_name, listing_id, title, url, last_price, rating, matched, first_seen_ts "
            "FROM seen"
        )
        params: tuple[Any, ...] = ()
        if item_name is not None:
            sql += " WHERE item_name = ?"
            params = (item_name,)
        sql += " ORDER BY first_seen_ts DESC"
        cols = [
            "item_name",
            "listing_id",
            "title",
            "url",
            "last_price",
            "rating",
            "matched",
            "first_seen_ts",
        ]
        return [dict(zip(cols, row, strict=True)) for row in self._conn.execute(sql, params)]
