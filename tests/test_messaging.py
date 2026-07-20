"""Tests for offer math, the draft store, and the message drafter (with fakes)."""

from __future__ import annotations

from pathlib import Path

from deal_radar.config.schema import ItemConfig, MessagingConfig
from deal_radar.messaging.drafter import MessageDrafter
from deal_radar.messaging.offer import compute_offer
from deal_radar.messaging.store import SqliteDraftStore
from deal_radar.models import Evaluation, Listing

# --- compute_offer -----------------------------------------------------------


def test_offer_basic_percent() -> None:
    assert compute_offer(100.0, 90) == 90


def test_offer_rounds_to_nearest_five() -> None:
    assert compute_offer(87.0, 90) == 80  # 78.3 -> 80
    assert compute_offer(86.0, 90) == 75  # 77.4 -> 75


def test_offer_half_rounds_up() -> None:
    assert compute_offer(250.0, 89) == 225  # 222.5 sits exactly between 220 and 225


def test_offer_never_above_asking() -> None:
    assert compute_offer(21.0, 100) == 20  # 21 -> nearest $5 is 20, cap not needed
    assert compute_offer(23.0, 100) == 23  # 23 -> rounds to 25, clamped to asking


def test_offer_none_for_unknown_or_free_price() -> None:
    assert compute_offer(None, 90) is None
    assert compute_offer(0.0, 90) is None


def test_offer_none_when_rounds_to_zero() -> None:
    assert compute_offer(2.0, 50) is None  # 1.0 -> rounds to 0


# --- SqliteDraftStore ---------------------------------------------------------


def _listing(listing_id: str = "1", price: float | None = 500.0) -> Listing:
    return Listing(
        id=listing_id,
        marketplace="facebook",
        title="RTX PC",
        url=f"u/{listing_id}",
        price=price,
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / "drafts.sqlite3"


def test_store_add_get_list(tmp_path: Path) -> None:
    with SqliteDraftStore(_db(tmp_path)) as store:
        draft_id = store.add_draft(
            item_name="PC", listing=_listing(), message="hi", offer_price=450
        )
        row = store.get(draft_id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["offer_price"] == 450
        assert row["message"] == "hi"
        assert row["asking_price"] == 500.0
        assert [r["id"] for r in store.list_drafts()] == [draft_id]
        assert store.list_drafts(status="sent") == []


def test_store_duplicate_listing_ignored(tmp_path: Path) -> None:
    with SqliteDraftStore(_db(tmp_path)) as store:
        first = store.add_draft(item_name="PC", listing=_listing(), message="a", offer_price=None)
        second = store.add_draft(item_name="PC", listing=_listing(), message="b", offer_price=90)
        assert first == second
        row = store.get(first)
        assert row is not None
        assert row["message"] == "a"  # original draft kept


def test_store_lifecycle_and_edited_message(tmp_path: Path) -> None:
    with SqliteDraftStore(_db(tmp_path)) as store:
        draft_id = store.add_draft(
            item_name="PC", listing=_listing(), message="hi", offer_price=None
        )
        created = store.get(draft_id)
        store.set_status(draft_id, "sending", message="hi (edited)")
        store.set_status(draft_id, "failed", error="kaboom")
        row = store.get(draft_id)
        assert row is not None and created is not None
        assert row["status"] == "failed"
        assert row["message"] == "hi (edited)"
        assert row["error"] == "kaboom"
        assert row["updated_ts"] >= created["updated_ts"]


def test_store_recovers_interrupted_sends_on_open(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with SqliteDraftStore(db) as store:
        draft_id = store.add_draft(
            item_name="PC", listing=_listing(), message="hi", offer_price=None
        )
        store.set_status(draft_id, "sending")
    with SqliteDraftStore(db) as store:  # simulated process restart
        row = store.get(draft_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == "interrupted"


# --- MessageDrafter ------------------------------------------------------------


class FakeComposer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def compose(self, item: ItemConfig, listing: Listing, offer_price: int | None) -> str:
        self.calls.append((listing.id, offer_price))
        return f"msg for {listing.id}"


def _item(**kw: object) -> ItemConfig:
    base: dict[str, object] = {
        "name": "PC",
        "marketplaces": ["facebook"],
        "search_phrases": ["pc"],
        "description": "want an rtx desktop",
    }
    base.update(kw)
    return ItemConfig(**base)  # type: ignore[arg-type]


def _eval() -> Evaluation:
    return Evaluation(match=True, rating=5, rationale="r", model="m")


def test_drafter_no_negotiate_means_no_offer(tmp_path: Path) -> None:
    composer = FakeComposer()
    with SqliteDraftStore(_db(tmp_path)) as store:
        drafter = MessageDrafter(MessagingConfig(enabled=True), composer, store)
        drafter.draft(_item(), _listing(), _eval())
        assert composer.calls == [("1", None)]
        assert store.list_drafts()[0]["offer_price"] is None


def test_drafter_negotiate_computes_offer(tmp_path: Path) -> None:
    composer = FakeComposer()
    messaging = MessagingConfig(enabled=True, negotiate=True, offer_percent=90)
    with SqliteDraftStore(_db(tmp_path)) as store:
        drafter = MessageDrafter(messaging, composer, store)
        drafter.draft(_item(), _listing(price=500.0), _eval())
        assert composer.calls == [("1", 450)]
        row = store.list_drafts()[0]
        assert row["offer_price"] == 450
        assert row["message"] == "msg for 1"


def test_drafter_item_overrides_win(tmp_path: Path) -> None:
    composer = FakeComposer()
    messaging = MessagingConfig(enabled=True, negotiate=False, offer_percent=90)
    with SqliteDraftStore(_db(tmp_path)) as store:
        drafter = MessageDrafter(messaging, composer, store)
        drafter.draft(_item(negotiate=True, offer_percent=80), _listing(price=500.0), _eval())
        assert composer.calls == [("1", 400)]


def test_drafter_unknown_price_still_drafts(tmp_path: Path) -> None:
    composer = FakeComposer()
    messaging = MessagingConfig(enabled=True, negotiate=True)
    with SqliteDraftStore(_db(tmp_path)) as store:
        drafter = MessageDrafter(messaging, composer, store)
        drafter.draft(_item(), _listing(price=None), _eval())
        assert composer.calls == [("1", None)]  # availability-only, never invents a number
        assert store.list_drafts()[0]["offer_price"] is None
