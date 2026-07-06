"""Tests for scan_item orchestration and the filter helpers (with fakes)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from deal_radar.config.schema import AIConfig, AppConfig, ItemConfig, MarketplaceConfig
from deal_radar.marketplaces.base import SearchContext
from deal_radar.models import Evaluation, Listing, NotificationEvent
from deal_radar.pipeline import passes_keyword_filters, scan_all, scan_item, within_price

AI = AIConfig(min_rating=4)


class FakeMarket:
    name = "fake"

    def __init__(self, listings: list[Listing], *, detail: dict[str, str] | None = None) -> None:
        self._listings = listings
        self._detail = detail or {}
        self.detail_calls: list[str] = []

    def __enter__(self) -> FakeMarket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def search(self, item: ItemConfig, ctx: SearchContext) -> Iterator[Listing]:
        yield from self._listings

    def fetch_details(self, listing: Listing) -> Listing:
        self.detail_calls.append(listing.id)
        text = self._detail.get(listing.id)
        if text is None:
            return listing
        return replace(listing, description=text)


class FakeStore:
    def __init__(self) -> None:
        self.seen: set[tuple[str, str]] = set()
        self.marked: list[tuple[str, Evaluation | None]] = []

    def is_seen(self, item_name: str, listing_id: str) -> bool:
        return (item_name, listing_id) in self.seen

    def mark_seen(
        self, item_name: str, listing: Listing, evaluation: Evaluation | None = None
    ) -> None:
        self.seen.add((item_name, listing.id))
        self.marked.append((listing.id, evaluation))

    def last_price(self, item_name: str, listing_id: str) -> float | None:
        return None


class FakeEval:
    def __init__(self, mapping: dict[str, Evaluation]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def evaluate(self, item: ItemConfig, listing: Listing) -> Evaluation:
        self.calls.append(listing.id)
        return self.mapping[listing.id]


class FakeNotifier:
    type = "fake"

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def notify(self, event: NotificationEvent) -> None:
        self.events.append(event)


def _item(**kw: Any) -> ItemConfig:
    base: dict[str, Any] = {
        "name": "PC",
        "marketplaces": ["fake"],
        "search_phrases": ["pc"],
        "description": "want an rtx desktop",
    }
    base.update(kw)
    return ItemConfig(**base)


def _listing(listing_id: str, title: str = "RTX PC", price: float | None = 500.0) -> Listing:
    return Listing(
        id=listing_id, marketplace="fake", title=title, url=f"u/{listing_id}", price=price
    )


def _ctx() -> SearchContext:
    return SearchContext(config=MarketplaceConfig())


def _good(rating: int = 5) -> Evaluation:
    return Evaluation(match=True, rating=rating, rationale="r", model="m")


def test_match_notifies() -> None:
    ev = FakeEval({"1": _good()})
    store, notifier = FakeStore(), FakeNotifier()
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1")]),
        ctx=_ctx(),
        evaluator=ev,
        store=store,
        notifiers=[notifier],
        ai=AI,
    )
    assert stats.matched == 1
    assert stats.notified == 1
    assert len(notifier.events) == 1
    assert store.is_seen("PC", "1")


def test_below_threshold_not_notified() -> None:
    ev = FakeEval({"1": _good(rating=3)})
    notifier = FakeNotifier()
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1")]),
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[notifier],
        ai=AI,
    )
    assert stats.matched == 0
    assert not notifier.events


def test_seen_is_skipped_before_eval() -> None:
    store = FakeStore()
    store.seen.add(("PC", "1"))
    ev = FakeEval({})
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1")]),
        ctx=_ctx(),
        evaluator=ev,
        store=store,
        notifiers=[],
        ai=AI,
    )
    assert stats.skipped_seen == 1
    assert ev.calls == []


def test_exclude_keyword_filters_and_marks_seen() -> None:
    ev = FakeEval({})
    store = FakeStore()
    stats = scan_item(
        item=_item(exclude_keywords=["broken"]),
        marketplace=FakeMarket([_listing("1", title="broken RTX PC")]),
        ctx=_ctx(),
        evaluator=ev,
        store=store,
        notifiers=[],
        ai=AI,
    )
    assert stats.skipped_filter == 1
    assert ev.calls == []
    assert store.is_seen("PC", "1")


def test_price_out_of_range_filters() -> None:
    ev = FakeEval({})
    stats = scan_item(
        item=_item(price_max=400),
        marketplace=FakeMarket([_listing("1", price=900.0)]),
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[],
        ai=AI,
    )
    assert stats.skipped_filter == 1
    assert ev.calls == []


def test_dry_run_counts_match_but_does_not_notify() -> None:
    ev = FakeEval({"1": _good()})
    notifier = FakeNotifier()
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1")]),
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[notifier],
        ai=AI,
        dry_run=True,
    )
    assert stats.matched == 1
    assert stats.notified == 0
    assert not notifier.events


def test_eval_error_counted_and_scan_continues() -> None:
    class BoomEval:
        def evaluate(self, item: ItemConfig, listing: Listing) -> Evaluation:
            raise RuntimeError("boom")

    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1"), _listing("2")]),
        ctx=_ctx(),
        evaluator=BoomEval(),
        store=FakeStore(),
        notifiers=[],
        ai=AI,
    )
    assert stats.errors == 2
    assert stats.evaluated == 0


def test_max_evaluations_caps_calls() -> None:
    listings = [_listing(str(i)) for i in range(5)]
    ev = FakeEval({str(i): _good(rating=1) for i in range(5)})
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket(listings),
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[],
        ai=AI,
        max_evaluations=2,
    )
    assert stats.evaluated == 2


def test_keyword_filter_helper() -> None:
    # include_keywords are a soft signal now: only exclude_keywords hard-filter.
    item = _item(include_keywords=["rtx"], exclude_keywords=["broken"])
    assert passes_keyword_filters(_listing("1", title="RTX 3070"), item)
    # No longer rejected for missing an include keyword — the AI judges the match.
    assert passes_keyword_filters(_listing("1", title="GTX 1050"), item)
    assert not passes_keyword_filters(_listing("1", title="broken RTX"), item)


def test_within_price_helper() -> None:
    item = _item(price_min=400, price_max=1000)
    assert within_price(_listing("1", price=500.0), item)
    assert not within_price(_listing("1", price=200.0), item)
    assert within_price(_listing("1", price=None), item)


def _app_config(items: list[ItemConfig]) -> AppConfig:
    return AppConfig(
        ai=AI,
        marketplaces={"fake": MarketplaceConfig()},
        notifiers=[{"type": "ntfy", "topic": "t"}],
        items=items,
    )


def test_scan_all_scans_each_item_and_reports_stats() -> None:
    item = _item()
    cfg = _app_config([item])
    market = FakeMarket([_listing("1"), _listing("2")])
    ev = FakeEval({"1": _good(), "2": _good(rating=2)})
    store, notifier = FakeStore(), FakeNotifier()
    seen_stats: list[str] = []

    results = scan_all(
        cfg=cfg,
        items=[item],
        make_marketplace=lambda name, mk_cfg: market,
        evaluator=ev,
        store=store,
        notifiers=[notifier],
        on_stats=lambda s: seen_stats.append(s.item),
    )
    assert len(results) == 1
    assert results[0].found == 2
    assert results[0].matched == 1  # only "1" clears the rating-4 threshold
    assert seen_stats == ["PC"]


def test_scan_all_skips_disabled_marketplace() -> None:
    item = _item()
    cfg = _app_config([item])
    cfg.marketplaces["fake"].enabled = False
    calls = {"n": 0}

    def make(name: str, mk_cfg: MarketplaceConfig) -> FakeMarket:
        calls["n"] += 1
        return FakeMarket([_listing("1")])

    results = scan_all(
        cfg=cfg,
        items=[item],
        make_marketplace=make,
        evaluator=FakeEval({}),
        store=FakeStore(),
        notifiers=[],
    )
    assert results == []
    assert calls["n"] == 0  # never built the disabled marketplace


class _CapturingEval:
    """Records the listing it was asked to evaluate."""

    def __init__(self, verdict: Evaluation) -> None:
        self.verdict = verdict
        self.seen: list[Listing] = []

    def evaluate(self, item: ItemConfig, listing: Listing) -> Evaluation:
        self.seen.append(listing)
        return self.verdict


def test_fetch_details_enriches_description_before_eval() -> None:
    market = FakeMarket(
        [_listing("1", title="Gaming PC")],
        detail={"1": "Intel i9-12900K, RTX 3070, 32GB DDR5, 850W PSU"},
    )
    ev = _CapturingEval(_good())
    scan_item(
        item=_item(),
        marketplace=market,
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[FakeNotifier()],
        ai=AI,
    )
    assert market.detail_calls == ["1"]
    assert "RTX 3070" in ev.seen[0].description  # AI judged the full body, not the card


def test_detail_text_can_reveal_an_exclusion() -> None:
    # Card text is clean; the exclusion only appears in the detail-page body.
    market = FakeMarket([_listing("1", title="Gaming PC")], detail={"1": "Selling for parts only"})
    ev = FakeEval({})
    store = FakeStore()
    stats = scan_item(
        item=_item(exclude_keywords=["for parts"]),
        marketplace=market,
        ctx=_ctx(),
        evaluator=ev,
        store=store,
        notifiers=[],
        ai=AI,
    )
    assert stats.skipped_filter == 1
    assert ev.calls == []  # excluded after enrichment, before spending an AI eval
    assert store.is_seen("PC", "1")


def test_fetch_details_disabled_skips_enrichment() -> None:
    market = FakeMarket([_listing("1")], detail={"1": "should not be used"})
    ev = _CapturingEval(_good())
    ctx = SearchContext(config=MarketplaceConfig(fetch_details=False))
    scan_item(
        item=_item(),
        marketplace=market,
        ctx=ctx,
        evaluator=ev,
        store=FakeStore(),
        notifiers=[FakeNotifier()],
        ai=AI,
    )
    assert market.detail_calls == []
    assert ev.seen[0].description == ""  # card text, not the detail body


def test_scan_item_should_stop_halts_before_first_listing() -> None:
    ev = FakeEval({})
    stats = scan_item(
        item=_item(),
        marketplace=FakeMarket([_listing("1"), _listing("2")]),
        ctx=_ctx(),
        evaluator=ev,
        store=FakeStore(),
        notifiers=[],
        ai=AI,
        should_stop=lambda: True,
    )
    assert stats.found == 0  # bailed before consuming any listing
    assert ev.calls == []


def test_scan_all_should_stop_skips_items() -> None:
    item = _item()
    cfg = _app_config([item])
    calls = {"n": 0}

    def make(name: str, mk_cfg: MarketplaceConfig) -> FakeMarket:
        calls["n"] += 1
        return FakeMarket([_listing("1")])

    results = scan_all(
        cfg=cfg,
        items=[item],
        make_marketplace=make,
        evaluator=FakeEval({}),
        store=FakeStore(),
        notifiers=[],
        should_stop=lambda: True,
    )
    assert results == []
    assert calls["n"] == 0  # stopped before building any marketplace
