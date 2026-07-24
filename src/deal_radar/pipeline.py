"""Per-item scan orchestration: search -> filter -> dedup -> AI eval -> notify."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .ai.base import Evaluator
from .config.schema import AIConfig, AppConfig, ItemConfig, MarketplaceConfig
from .dedup.base import SeenStore
from .logging import get_logger
from .marketplaces.base import Marketplace, SearchContext
from .messaging.drafter import Drafter
from .models import Evaluation, Listing, NotificationEvent
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
    drafted: int = 0
    notified: int = 0
    errors: int = 0


def _rank_key(pair: tuple[Listing, Evaluation]) -> tuple[int, float]:
    """Best-first sort key: highest rating first, then cheapest known price.

    Unknown prices sort last within a rating so a listing with a real (low)
    price is preferred over one whose price the card never exposed.
    """
    listing, evaluation = pair
    price = listing.price if listing.price is not None else float("inf")
    return (-evaluation.rating, price)


def format_stats(stats: ScanStats) -> str:
    """One-line human summary of an item scan, shared by the CLI and web runner."""
    return (
        f"{stats.item}: found={stats.found} new_seen_skipped={stats.skipped_seen} "
        f"filtered={stats.skipped_filter} evaluated={stats.evaluated} "
        f"matched={stats.matched} drafted={stats.drafted} notified={stats.notified} "
        f"errors={stats.errors}"
    )


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
    drafter: Drafter | None = None,
    max_evaluations: int | None = None,
    notify_top_n: int = 5,
    dry_run: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> ScanStats:
    """Scan one item on one marketplace and notify on the best new matches.

    Every qualifying listing (a match at or above the rating threshold) is
    collected during the pass; once the whole feed has been scanned they are
    ranked best-first and only the top ``notify_top_n`` are sent, as a single
    ranked digest per notifier rather than one alert per match.
    """
    stats = ScanStats(item=item.name)
    threshold = item.effective_min_rating(ai)
    matches: list[tuple[Listing, Evaluation]] = []

    for listing in marketplace.search(item, ctx):
        if should_stop is not None and should_stop():
            log.info("stop requested; halting scan of %s", item.name)
            break
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

        if ctx.config.fetch_details:
            # Enrich with the full detail-page body so the AI judges on real specs,
            # not the sparse card. Richer text may also reveal an exclusion the card hid.
            listing = marketplace.fetch_details(listing)
            if not passes_keyword_filters(listing, item):
                stats.skipped_filter += 1
                store.mark_seen(item.name, listing)
                continue

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
        matches.append((listing, evaluation))

    _notify_best(
        item=item,
        matches=matches,
        notifiers=notifiers,
        drafter=drafter,
        notify_top_n=notify_top_n,
        dry_run=dry_run,
        stats=stats,
    )
    return stats


def _notify_best(
    *,
    item: ItemConfig,
    matches: list[tuple[Listing, Evaluation]],
    notifiers: Sequence[Notifier],
    drafter: Drafter | None,
    notify_top_n: int,
    dry_run: bool,
    stats: ScanStats,
) -> None:
    """Rank the pass's matches and send the top ``notify_top_n`` as one digest."""
    if not matches:
        return
    matches.sort(key=_rank_key)
    top = matches[:notify_top_n]
    log.info(
        "%s: %d match(es) this pass; notifying top %d",
        item.name,
        len(matches),
        len(top),
    )
    if dry_run:
        return

    events: list[NotificationEvent] = []
    for listing, evaluation in top:
        draft_pending = False
        if drafter is not None:
            try:
                drafter.draft(item, listing, evaluation)
                stats.drafted += 1
                draft_pending = True
            except Exception as exc:  # noqa: BLE001 - a failed draft shouldn't kill the scan
                stats.errors += 1
                log.warning("drafting a seller message for %s failed: %s", listing.id, exc)
        events.append(
            NotificationEvent(
                item_name=item.name,
                listing=listing,
                evaluation=evaluation,
                draft_pending=draft_pending,
            )
        )

    for notifier in notifiers:
        try:
            notifier.notify_digest(item.name, events)
            stats.notified += len(events)
        except Exception as exc:  # noqa: BLE001 - one failed notifier shouldn't kill the scan
            stats.errors += 1
            log.warning("notify via %s failed: %s", notifier.type, exc)


def scan_all(
    *,
    cfg: AppConfig,
    items: Sequence[ItemConfig],
    make_marketplace: Callable[[str, MarketplaceConfig], Marketplace],
    evaluator: Evaluator,
    store: SeenStore,
    notifiers: Sequence[Notifier],
    drafter: Drafter | None = None,
    max_evaluations: int | None = None,
    dry_run: bool = False,
    on_stats: Callable[[ScanStats], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[ScanStats]:
    """Run one full pass over every selected item on each enabled marketplace.

    A fresh marketplace adapter is built (and its browser opened) per pass via
    ``make_marketplace`` so long-running loops don't hold a browser session open
    for hours. ``on_stats`` is called with each item's :class:`ScanStats` as it
    completes (e.g. to print progress). ``should_stop`` is polled between items
    (and forwarded to each item scan) for cooperative cancellation.
    """
    needed = {
        m
        for item in items
        for m in item.marketplaces
        if m in cfg.marketplaces and cfg.marketplaces[m].enabled
    }
    results: list[ScanStats] = []
    for mname in sorted(needed):
        if should_stop is not None and should_stop():
            break
        mk_cfg = cfg.marketplaces[mname]
        marketplace = make_marketplace(mname, mk_cfg)
        with marketplace:
            ctx = SearchContext(config=mk_cfg, dry_run=dry_run)
            for item in items:
                if should_stop is not None and should_stop():
                    break
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
                    drafter=drafter,
                    max_evaluations=max_evaluations,
                    notify_top_n=cfg.notify_top_n,
                    dry_run=dry_run,
                    should_stop=should_stop,
                )
                results.append(stats)
                if on_stats is not None:
                    on_stats(stats)
    return results
