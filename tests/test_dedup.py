"""Tests for the SQLite seen store."""

from __future__ import annotations

from pathlib import Path

from deal_radar.dedup.sqlite_store import SqliteSeenStore
from deal_radar.models import Evaluation, Listing


def _listing(listing_id: str = "1", price: float | None = 500.0) -> Listing:
    return Listing(id=listing_id, marketplace="facebook", title="Gaming PC", url="u", price=price)


def test_seen_roundtrip(tmp_path: Path) -> None:
    with SqliteSeenStore(tmp_path / "s.db") as store:
        assert not store.is_seen("Item", "1")
        store.mark_seen("Item", _listing())
        assert store.is_seen("Item", "1")
        assert store.last_price("Item", "1") == 500.0


def test_mark_seen_with_evaluation_updates_rating(tmp_path: Path) -> None:
    with SqliteSeenStore(tmp_path / "s.db") as store:
        store.mark_seen("Item", _listing())  # no evaluation yet
        store.mark_seen(
            "Item",
            _listing(price=450.0),
            Evaluation(match=True, rating=5, rationale="x", model="m"),
        )
        rows = store.list_seen("Item")
    assert len(rows) == 1
    assert rows[0]["rating"] == 5
    assert rows[0]["matched"] == 1
    assert rows[0]["last_price"] == 450.0
    assert rows[0]["images_analyzed"] == 0


def test_mark_seen_records_images_analyzed(tmp_path: Path) -> None:
    with SqliteSeenStore(tmp_path / "s.db") as store:
        store.mark_seen(
            "Item",
            _listing(),
            Evaluation(match=True, rating=5, rationale="x", model="m", images_analyzed=True),
        )
        rows = store.list_seen("Item")
    assert rows[0]["images_analyzed"] == 1


def test_persists_across_connections(tmp_path: Path) -> None:
    p = tmp_path / "s.db"
    with SqliteSeenStore(p) as store:
        store.mark_seen("Item", _listing())
    with SqliteSeenStore(p) as store:
        assert store.is_seen("Item", "1")


def test_scoped_by_item(tmp_path: Path) -> None:
    with SqliteSeenStore(tmp_path / "s.db") as store:
        store.mark_seen("A", _listing("1"))
        assert store.is_seen("A", "1")
        assert not store.is_seen("B", "1")
