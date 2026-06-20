"""The dedup-store interface. SQLite implementation arrives in Phase 1."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Evaluation, Listing


@runtime_checkable
class SeenStore(Protocol):
    """Tracks which listings have already been seen/notified, across runs."""

    def is_seen(self, item_name: str, listing_id: str) -> bool: ...

    def mark_seen(
        self, item_name: str, listing: Listing, evaluation: Evaluation | None = None
    ) -> None: ...

    def last_price(self, item_name: str, listing_id: str) -> float | None: ...
