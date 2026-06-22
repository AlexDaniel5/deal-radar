"""Per-item scan orchestration: search -> filter -> dedup -> AI eval -> notify."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .ai.base import Evaluator
from .config.schema import AIConfig, AppConfig, ItemConfig, MarketplaceConfig
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
    """Hard-filter a listing on exclude_keywords only.

    ``include_keywords`` are deliberately NOT a hard AND filter: a marketplace
    card's text is sparse (component details like 'carbon' or 'rtx' live on the
    item's detail page, not the card), so requiring all/any of them here rejected
    genuine matches. Includes are now a soft signal — the AI evaluator judges the
    real 'match' from the item description; only obvious junk (exclude_keywords)
    and out-of-range prices are filtered cheaply up front.
    """
    haystack = f"{listing.title}\n{listing.description}".lower()
    return not any(kw.lower() in haystack for kw in item.exclude_keywords)


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


def scan_all(
    *,
    cfg: AppConfig,
    items: Sequence[ItemConfig],
    make_marketplace: Callable[[str, MarketplaceConfig], Marketplace],
    evaluator: Evaluator,
    store: SeenStore,
    notifiers: Sequence[Notifier],
    max_evaluations: int | None = None,
    dry_run: bool = False,
    on_stats: Callable[[ScanStats], None] | None = None,
) -> list[ScanStats]:
    """Run one full pass over every selected item on each enabled marketplace.

    A fresh marketplace adapter is built (and its browser opened) per pass via
    ``make_marketplace`` so long-running loops don't hold a browser session open
    for hours. ``on_stats`` is called with each item's :class:`ScanStats` as it
    completes (e.g. to print progress).
    """
    needed = {
        m
        for item in items
        for m in item.marketplaces
        if m in cfg.marketplaces and cfg.marketplaces[m].enabled
    }
    results: list[ScanStats] = []
    for mname in sorted(needed):
        mk_cfg = cfg.marketplaces[mname]
        marketplace = make_marketplace(mname, mk_cfg)
        with marketplace:
            ctx = SearchContext(config=mk_cfg, dry_run=dry_run)
            for item in items:
                if mname not in item.marketplaces:
                    continue
                stats = scan_item(
                    item=item,
                    marketplace=marketplace,
                    ctx=ctx,
                    evaluator=evaluator,
                    store=store,
                    notifiers=notifiers,
                    ai=cfg.ai,
                    max_evaluations=max_evaluations,
                    dry_run=dry_run,
                )
                results.append(stats)
                if on_stats is not None:
                    on_stats(stats)
    return results
