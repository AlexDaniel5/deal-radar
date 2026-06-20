"""Per-item scan orchestration: search -> filter -> dedup -> AI eval -> notify."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .ai.base import Evaluator
from .config.schema import AIConfig, ItemConfig
from .dedup.base import SeenStore
from .logging import get_logger
from .marketplaces.base import Marketplace, SearchContext
from .models import Listing, NotificationEvent
from .notifiers.base import Notifier

log = get_logger("pipeline")


@dataclass
class ScanStats:
    """Outcome counters for one item scan."""

    item: str
    found: int = 0
    skipped_seen: int = 0
    skipped_filter: int = 0
    evaluated: int = 0
    matched: int = 0
    notified: int = 0
    errors: int = 0


def passes_keyword_filters(listing: Listing, item: ItemConfig) -> bool:
    """Apply include/exclude keyword rules against the listing title + description."""
    haystack = f"{listing.title}\n{listing.description}".lower()
    if any(kw.lower() in haystack for kw in item.exclude_keywords):
        return False
    if item.include_keywords:
        return any(kw.lower() in haystack for kw in item.include_keywords)
    return True


def within_price(listing: Listing, item: ItemConfig) -> bool:
    """Price-range check. Unknown prices pass through to let the AI judge."""
    if listing.price is None:
        return True
    if item.price_min is not None and listing.price < item.price_min:
        return False
    if item.price_max is not None and listing.price > item.price_max:
        return False
    return True


def scan_item(
    *,
    item: ItemConfig,
    marketplace: Marketplace,
    ctx: SearchContext,
    evaluator: Evaluator,
    store: SeenStore,
    notifiers: Sequence[Notifier],
    ai: AIConfig,
    max_evaluations: int | None = None,
    dry_run: bool = False,
) -> ScanStats:
    """Scan one item on one marketplace and notify on good new matches."""
    stats = ScanStats(item=item.name)
    threshold = item.effective_min_rating(ai)

    for listing in marketplace.search(item, ctx):
        stats.found += 1
        if store.is_seen(item.name, listing.id):
            stats.skipped_seen += 1
            continue
        if not within_price(listing, item) or not passes_keyword_filters(listing, item):
            stats.skipped_filter += 1
            store.mark_seen(item.name, listing)  # remember so we don't reconsider it
            continue
        if max_evaluations is not None and stats.evaluated >= max_evaluations:
            log.info("reached max_evaluations=%d for %s; stopping", max_evaluations, item.name)
            break

        try:
            evaluation = evaluator.evaluate(item, listing)
        except Exception as exc:  # noqa: BLE001 - one bad listing shouldn't kill the scan
            stats.errors += 1
            log.warning("evaluation failed for %s: %s", listing.id, exc)
            continue
        stats.evaluated += 1
        store.mark_seen(item.name, listing, evaluation)

        if not (evaluation.match and evaluation.rating >= threshold):
            continue
        stats.matched += 1
        log.info(
            "MATCH %s [%d/5]: %s (%s)",
            item.name,
            evaluation.rating,
            listing.title,
            listing.url,
        )
        if dry_run:
            continue
        event = NotificationEvent(item_name=item.name, listing=listing, evaluation=evaluation)
        for notifier in notifiers:
            try:
                notifier.notify(event)
                stats.notified += 1
            except Exception as exc:  # noqa: BLE001 - one failed notifier shouldn't kill the scan
                stats.errors += 1
                log.warning("notify via %s failed: %s", notifier.type, exc)

    return stats
