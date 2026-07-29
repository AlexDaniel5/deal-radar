"""Tests for scan progress reporting.

A scan can run for a quarter of an hour (page loads are paced ~25s apart, and
each candidate costs a second load), so "is it working or is it stuck?" needs a
real answer rather than a green dot.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from deal_radar.config.schema import AIConfig, ItemConfig, MarketplaceConfig
from deal_radar.marketplaces.base import SearchContext
from deal_radar.models import Evaluation, Listing
from deal_radar.pipeline import ProgressEvent, scan_all, scan_item
from deal_radar.web.app import create_app
from deal_radar.web.controller import ScannerController
from deal_radar.web.progress import ProgressTracker, humanize_eta

VALID_CONFIG = """version: 1
marketplaces: {facebook: {enabled: true}}
notifiers: [{type: ntfy, topic: t}]
items:
  - {name: PC, marketplaces: [facebook], search_phrases: [pc], description: d}
"""


def _listing(i: int) -> Listing:
    return Listing(
        id=str(i),
        marketplace="facebook",
        title=f"Listing {i}",
        url=f"https://example.com/{i}",
        price=100.0,
    )


class FakeMarketplace:
    def __init__(self, count: int = 3) -> None:
        self.count = count

    def __enter__(self) -> FakeMarketplace:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def search(self, item: Any, ctx: Any) -> list[Listing]:
        return [_listing(i) for i in range(self.count)]

    def fetch_details(self, listing: Listing) -> Listing:
        return listing


class FakeStore:
    def __init__(self) -> None:
        self.seen: set[tuple[str, str]] = set()

    def is_seen(self, item: str, listing_id: str) -> bool:
        return (item, listing_id) in self.seen

    def mark_seen(self, item: str, listing: Listing, evaluation: Any = None) -> None:
        self.seen.add((item, listing.id))

    def last_price(self, item: str, listing_id: str) -> float | None:
        return None


class FakeEvaluator:
    def evaluate(self, item: Any, listing: Listing) -> Evaluation:
        return Evaluation(match=True, rating=5, rationale="x", model="m")


def _item() -> ItemConfig:
    return ItemConfig(
        name="PC", marketplaces=["facebook"], search_phrases=["pc"], description="d"
    )


# --- the pipeline hook ---------------------------------------------------------


def test_progress_events_arrive_in_a_sensible_order() -> None:
    events: list[ProgressEvent] = []
    scan_item(
        item=_item(),
        marketplace=FakeMarketplace(2),
        ctx=SearchContext(config=MarketplaceConfig(fetch_details=True), dry_run=True),
        evaluator=FakeEvaluator(),
        store=FakeStore(),
        notifiers=[],
        ai=AIConfig(),
        on_progress=events.append,
    )
    phases = [e.phase for e in events]
    assert phases[0] == "searching"
    assert phases[-1] == "done"
    assert "found" in phases
    assert "fetching" in phases
    assert "evaluating" in phases
    # Every candidate reports being checked.
    assert phases.count("checked") == 2
    # Fetching a listing always precedes asking the AI about it.
    assert phases.index("fetching") < phases.index("evaluating")


def test_progress_carries_running_counts() -> None:
    events: list[ProgressEvent] = []
    scan_item(
        item=_item(),
        marketplace=FakeMarketplace(3),
        ctx=SearchContext(config=MarketplaceConfig(fetch_details=False), dry_run=True),
        evaluator=FakeEvaluator(),
        store=FakeStore(),
        notifiers=[],
        ai=AIConfig(),
        max_evaluations=3,
        on_progress=events.append,
    )
    final = events[-1]
    assert final.checked == 3
    assert final.of == 3
    assert final.found == 3
    assert final.matched == 3


def test_no_detail_fetch_means_no_fetching_phase() -> None:
    events: list[ProgressEvent] = []
    scan_item(
        item=_item(),
        marketplace=FakeMarketplace(1),
        ctx=SearchContext(config=MarketplaceConfig(fetch_details=False), dry_run=True),
        evaluator=FakeEvaluator(),
        store=FakeStore(),
        notifiers=[],
        ai=AIConfig(),
        on_progress=events.append,
    )
    assert "fetching" not in [e.phase for e in events]


def test_scan_all_numbers_the_items() -> None:
    """So the UI can say "2 of 3" rather than an open-ended count."""
    from deal_radar.config.loader import validate_config_text

    cfg = validate_config_text(
        VALID_CONFIG + "  - {name: Bike, marketplaces: [facebook], "
        "search_phrases: [bike], description: d}\n"
    )
    events: list[ProgressEvent] = []
    scan_all(
        cfg=cfg,
        items=list(cfg.items),
        make_marketplace=lambda name, mk: FakeMarketplace(1),  # type: ignore[arg-type,return-value]
        evaluator=FakeEvaluator(),
        store=FakeStore(),  # type: ignore[arg-type]
        notifiers=[],
        dry_run=True,
        on_progress=events.append,
    )
    assert {e.item_count for e in events} == {2}
    assert sorted({e.item_index for e in events}) == [1, 2]


def test_progress_is_optional() -> None:
    """The CLI passes nothing and must be unaffected."""
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarketplace(1),
        ctx=SearchContext(config=MarketplaceConfig(fetch_details=False), dry_run=True),
        evaluator=FakeEvaluator(),
        store=FakeStore(),
        notifiers=[],
        ai=AIConfig(),
    )
    assert stats.evaluated == 1


# --- the tracker ----------------------------------------------------------------


def test_tracker_is_empty_before_a_scan() -> None:
    assert ProgressTracker().snapshot() is None


def test_tracker_reports_the_latest_event() -> None:
    tracker = ProgressTracker()
    tracker.start(seconds_per_listing=50)
    tracker.record(
        ProgressEvent(
            phase="evaluating", item="PC", item_index=1, item_count=1, checked=3, of=10
        )
    )
    snap = tracker.snapshot()
    assert snap is not None
    assert snap["phase"] == "evaluating"
    assert snap["message"] == "Asking the AI about a listing"
    assert snap["checked"] == 3 and snap["of"] == 10
    assert snap["eta_seconds"] is not None


def test_tracker_eta_uses_the_configured_pacing_before_it_can_measure() -> None:
    tracker = ProgressTracker()
    tracker.start(seconds_per_listing=50)
    tracker.record(
        ProgressEvent(
            phase="evaluating", item="PC", item_index=1, item_count=1, checked=1, of=11
        )
    )
    snap = tracker.snapshot()
    assert snap is not None
    # 10 left x 50s: minutes, not seconds — which is the honest answer.
    assert snap["eta_seconds"] >= 400


def test_tracker_eta_is_none_without_a_cap() -> None:
    tracker = ProgressTracker()
    tracker.start(seconds_per_listing=25)
    tracker.record(ProgressEvent(phase="found", item="PC", found=4))
    snap = tracker.snapshot()
    assert snap is not None
    assert snap["eta_seconds"] is None


def test_tracker_clear_hides_the_bar() -> None:
    tracker = ProgressTracker()
    tracker.start(seconds_per_listing=25)
    tracker.record(ProgressEvent(phase="done", item="PC"))
    tracker.clear()
    assert tracker.snapshot() is None


def test_tracker_is_thread_safe() -> None:
    import threading

    tracker = ProgressTracker()
    tracker.start(seconds_per_listing=25)

    def hammer(n: int) -> None:
        for i in range(200):
            tracker.record(ProgressEvent(phase="evaluating", item=f"item{n}", checked=i))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker.snapshot() is not None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, ""),
        (10, "less than a minute left"),
        (300, "about 5 minutes left"),
        (60, "less than a minute left"),
        (7200, "about 2.0 hours left"),
    ],
)
def test_humanize_eta(seconds: int | None, expected: str) -> None:
    assert humanize_eta(seconds) == expected


def test_eta_reads_as_an_estimate_not_a_promise() -> None:
    """Wording matters: a precise-sounding countdown on a 25s-paced scan lies."""
    assert humanize_eta(300).startswith("about")


# --- the endpoint -----------------------------------------------------------------


def test_status_has_no_progress_when_idle(tmp_path: Any) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(lambda s: s.wait(), lambda s: None),
        login_fn=lambda *a, **k: None,
    )
    body = TestClient(app).get("/api/status").json()
    assert body["progress"] is None
    assert body["eta"] == ""


def test_status_exposes_progress_and_a_human_eta(tmp_path: Any) -> None:
    from deal_radar.web import progress as progress_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(lambda s: s.wait(), lambda s: None),
        login_fn=lambda *a, **k: None,
    )
    progress_mod.TRACKER.start(seconds_per_listing=50)
    progress_mod.TRACKER.record(
        ProgressEvent(
            phase="evaluating", item="PC", item_index=1, item_count=2, checked=2, of=10
        )
    )
    try:
        body = TestClient(app).get("/api/status").json()
        assert body["progress"]["item"] == "PC"
        assert body["progress"]["message"] == "Asking the AI about a listing"
        assert body["eta"].startswith("about") or body["eta"].startswith("less")
    finally:
        progress_mod.TRACKER.clear()
