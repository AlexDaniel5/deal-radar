"""SQLite-backed store for drafted seller messages awaiting approval."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..models import Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name    TEXT NOT NULL,
    listing_id   TEXT NOT NULL,
    marketplace  TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    asking_price REAL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    offer_price  INTEGER,
    message      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_ts   REAL NOT NULL,
    updated_ts   REAL NOT NULL,
    UNIQUE (item_name, listing_id)
);
"""

_COLS = (
    "id",
    "item_name",
    "listing_id",
    "marketplace",
    "title",
    "url",
    "asking_price",
    "currency",
    "offer_price",
    "message",
    "status",
    "error",
    "created_ts",
    "updated_ts",
)

# Lifecycle: pending -> sending -> sent | failed; pending|failed -> dismissed;
# failed -> sending (manual retry). One draft per (item, listing), ever.


class SqliteDraftStore:
    """Persists drafted seller messages and their approval/send lifecycle."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.parent and str(self._path.parent) not in ("", "."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Crash recovery: a send can't survive a process restart.
        self._conn.execute(
            "UPDATE drafts SET status = 'failed', error = 'interrupted', updated_ts = ? "
            "WHERE status = 'sending'",
            (time.time(),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteDraftStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_draft(
        self, *, item_name: str, listing: Listing, message: str, offer_price: int | None
    ) -> int:
        """Insert a pending draft; a listing already drafted for this item is left as-is.

        Returns the draft's row id (existing or new).
        """
        now = time.time()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO drafts
                (item_name, listing_id, marketplace, title, url, asking_price,
                 currency, offer_price, message, status, created_ts, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                item_name,
                listing.id,
                listing.marketplace,
                listing.title,
                listing.url,
                listing.price,
                listing.currency,
                offer_price,
                message,
                now,
                now,
            ),
        )
        self._conn.commit()
        cur = self._conn.execute(
            "SELECT id FROM drafts WHERE item_name = ? AND listing_id = ?",
            (item_name, listing.id),
        )
        return int(cur.fetchone()[0])

    def get(self, draft_id: int) -> dict[str, Any] | None:
        cur = self._conn.execute(f"SELECT {', '.join(_COLS)} FROM drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
        return dict(zip(_COLS, row, strict=True)) if row else None

    def list_drafts(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = f"SELECT {', '.join(_COLS)} FROM drafts"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_ts DESC LIMIT ?"
        params += (limit,)
        return [dict(zip(_COLS, row, strict=True)) for row in self._conn.execute(sql, params)]

    def set_status(
        self,
        draft_id: int,
        status: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        """Move a draft through its lifecycle; optionally record edited text or an error."""
        sets = ["status = ?", "updated_ts = ?", "error = ?"]
        params: list[Any] = [status, time.time(), error]
        if message is not None:
            sets.append("message = ?")
            params.append(message)
        params.append(draft_id)
        self._conn.execute(f"UPDATE drafts SET {', '.join(sets)} WHERE id = ?", params)
        self._conn.commit()
