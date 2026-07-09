"""Facebook Marketplace adapter (Playwright + persisted logged-in session).

DOM structure on Facebook changes often and is not contractual. The fragile
parsing lives in small pure helpers (`_parse_price`, `_extract_item_id`,
`_parse_card_text`, `build_search_url`) that are unit-tested; the Playwright I/O
around them is intentionally thin and will likely need selector tuning against
the live site.

Politeness: low volume, a configurable pause between page loads, a capped result
count, and a single logged-in account. No bot-detection evasion.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config.schema import ItemConfig, MarketplaceConfig
from ..errors import SearchError
from ..logging import get_logger
from ..models import Listing
from ..paths import default_session_path
from ..ratelimit import RateLimiter
from .base import SearchContext

log = get_logger("marketplace.facebook")

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ITEM_ANCHOR = 'a[href*="/marketplace/item/"]'
# Detail-page description containers, most specific first. The DOM is not
# contractual; tune these against live `detail id=...` DEBUG output if FB shifts.
_DETAIL_TEXT_SELECTORS = (
    '[data-testid="marketplace_pdp_description"]',
    'div[role="main"] div[data-testid="info_section"]',
)
# When the targeted selectors miss (FB reshuffles its obfuscated DOM often), fall
# back to the whole main content region: noisy, but it contains the real specs.
_DETAIL_MAIN_SELECTOR = 'div[role="main"]'
_DETAIL_MIN_USEFUL = 300  # chars below which we treat a targeted match as a stub
_DETAIL_MAX_CHARS = 2500  # cap the main-region fallback (keeps token cost sane)
# Stable English markers that begin the seller/ad section after the description;
# everything from the earliest match is page chrome, not the listing.
_DETAIL_TRAILING_MARKERS = ("Seller information", "Seller details")
# Status badges Facebook prepends to a card's text; never a real title.
_BADGE_RE = re.compile(r"^(just listed|new)$", re.IGNORECASE)


def _detect_currency(text: str) -> str:
    """Best-effort currency from a card's text. FB shows 'CA$' for CAD, '$' for USD."""
    if "CA$" in text or "C$" in text:
        return "CAD"
    return "USD"


def _pick_detail_text(candidates: Sequence[str]) -> str:
    """Choose the richest detail-page text: the longest non-empty candidate."""
    cleaned = [c.strip() for c in candidates if c and c.strip()]
    return max(cleaned, key=len) if cleaned else ""


def _safe_attr(page: Any, selector: str, name: str) -> str:
    try:
        return page.get_attribute(selector, name) or ""
    except Exception:  # noqa: BLE001 - element/attribute may be absent
        return ""


def _safe_inner_text(page: Any, selector: str) -> str:
    try:
        element = page.query_selector(selector)
        return element.inner_text().strip() if element is not None else ""
    except Exception:  # noqa: BLE001 - selector may not match this page
        return ""


def _trim_main_text(text: str) -> str:
    """Drop the seller/ad chrome that follows the description in the main region."""
    low = text.lower()
    cut = len(text)
    for marker in _DETAIL_TRAILING_MARKERS:
        found = low.find(marker.lower())
        if found != -1:
            cut = min(cut, found)
    return text[:cut].strip()


def _expand_description(page: Any) -> None:
    """Best-effort: click a 'See more' toggle so the full description renders."""
    try:
        page.get_by_text("See more", exact=False).first.click(timeout=2000)
    except Exception:  # noqa: BLE001 - no toggle / not clickable is fine
        pass


def _extract_detail_text(page: Any) -> str:
    """Pull the listing description from a detail page (best-effort, may be empty).

    Prefers a targeted description container; falls back to the whole main content
    region (noisy but contains the specs) when the selectors miss — which they will
    whenever Facebook reshuffles its obfuscated DOM. Tune ``_DETAIL_TEXT_SELECTORS``
    against the ``detail id=... text[...]`` DEBUG output from a live run.
    """
    candidates: list[str] = []
    og = _safe_attr(page, 'meta[property="og:description"]', "content")
    if og:
        candidates.append(og)
    for selector in _DETAIL_TEXT_SELECTORS:
        text = _safe_inner_text(page, selector)
        if text:
            candidates.append(text)

    best = _pick_detail_text(candidates)
    if len(best) >= _DETAIL_MIN_USEFUL:
        return best  # a targeted container matched cleanly; use it as-is

    # Targeted selectors missed (or gave only a stub) — fall back to the main region.
    main = _safe_inner_text(page, _DETAIL_MAIN_SELECTOR)
    if main:
        candidates.append(_trim_main_text(main)[:_DETAIL_MAX_CHARS])
    return _pick_detail_text(candidates)


def _extract_image_urls(page: Any) -> list[str]:
    """Collect the listing's photo URLs from a detail page (best-effort).

    Marketplace photos are served from the scontent CDN inside the main region;
    seller avatars and UI chrome are skipped by rendered size. Order is kept
    (cover photo first) and duplicates dropped. Tune against the
    ``detail id=... images=...`` DEBUG output from a live run.
    """
    urls: list[str] = []
    try:
        for img in page.query_selector_all(f'{_DETAIL_MAIN_SELECTOR} img[src*="scontent"]'):
            src = img.get_attribute("src") or ""
            if not src or src in urls:
                continue
            box = img.bounding_box()
            if box is not None and (box["width"] < 80 or box["height"] < 80):
                continue  # avatar / sprite / map tile
            urls.append(src)
    except Exception:  # noqa: BLE001 - best-effort; a photo-less listing is fine
        pass
    return urls


def _extract_item_id(href: str) -> str | None:
    match = _ITEM_ID_RE.search(href)
    return match.group(1) if match else None


def _parse_price(text: str) -> float | None:
    """Parse a price token like '$1,200', 'CA$950.00', 'Free'. Returns None if absent."""
    low = text.strip().lower()
    if not low:
        return None
    if low in {"free", "$0", "0"} or "free" in low.split():
        return 0.0
    match = _PRICE_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_card_text(text: str) -> tuple[float | None, str, str | None]:
    """Best-effort split of a listing card's text into (price, title, location).

    Observed card format is::

        Just listed | CA$<price> [| CA$<orig price>] | <TITLE> | <City, ST>

    i.e. an optional status badge, the asking price (plus an optional
    struck-through original price on sale items), the title, then the location.
    The title is whatever sits between the price block and the trailing location.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, "", None

    # Asking price = first price line; sale items add a struck-through original.
    price: float | None = None
    price_indices: list[int] = []
    for i, ln in enumerate(lines):
        if "$" in ln or "free" in ln.lower():
            parsed = _parse_price(ln)
            if parsed is not None:
                price_indices.append(i)
                if price is None:
                    price = parsed

    last_price_idx = price_indices[-1] if price_indices else -1
    location = lines[-1] if len(lines) > 1 else None
    location_idx = len(lines) - 1 if location is not None else len(lines)

    # Title sits after the price block and before the location, skipping badges.
    title_candidates = [
        ln
        for i, ln in enumerate(lines)
        if i > last_price_idx and i != location_idx and not _BADGE_RE.match(ln)
    ]
    if not title_candidates:
        # Sparse/odd card: fall back to any non-price, non-badge, non-location line.
        title_candidates = [
            ln
            for i, ln in enumerate(lines)
            if i not in price_indices and i != location_idx and not _BADGE_RE.match(ln)
        ]
    title = title_candidates[0] if title_candidates else lines[0]
    return price, title, location


def build_search_url(query: str, item: ItemConfig, marketplace: MarketplaceConfig) -> str:
    """Build a Facebook Marketplace search URL for one query phrase."""
    params: dict[str, str] = {
        "query": query,
        "sortBy": "creation_time_descend",
        "exact": "false",
    }
    price_min = item.price_min
    price_max = item.price_max
    if price_min is not None:
        params["minPrice"] = str(int(price_min))
    if price_max is not None:
        params["maxPrice"] = str(int(price_max))
    radius = item.radius_km if item.radius_km is not None else marketplace.default_radius_km
    if radius is not None:
        params["radius"] = str(int(radius))
    return "https://www.facebook.com/marketplace/search/?" + urllib.parse.urlencode(params)


def _resolve_session_path(config: MarketplaceConfig) -> Path:
    return Path(config.session_path) if config.session_path else default_session_path("facebook")


class FacebookMarketplace:
    """Searches Facebook Marketplace using a persisted logged-in browser session.

    Use as a context manager so the browser is opened once per run::

        with FacebookMarketplace(cfg) as mk:
            for listing in mk.search(item, ctx):
                ...
    """

    name = "facebook"

    def __init__(
        self,
        config: MarketplaceConfig,
        *,
        max_results: int = 40,
        headless: bool = True,
        page_timeout_ms: int = 30_000,
        scrolls: int = 2,
        pause: RateLimiter | None = None,
    ) -> None:
        self._config = config
        self._session_path = _resolve_session_path(config)
        self._max_results = max_results
        self._headless = headless
        self._page_timeout_ms = page_timeout_ms
        self._scrolls = scrolls
        self._pause = pause if pause is not None else RateLimiter(4.0, 3.0)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> FacebookMarketplace:
        if not self._session_path.is_file():
            raise SearchError(
                f"no saved Facebook session at {self._session_path}; "
                "run 'deal-radar login facebook' first"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional browser dep
            raise SearchError(
                "playwright is not available; install it and run 'playwright install chromium'"
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(storage_state=str(self._session_path))
        return self

    def __exit__(self, *exc: object) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        if self._pw is not None:
            self._pw.stop()
        self._context = self._browser = self._pw = None

    def search(self, item: ItemConfig, ctx: SearchContext) -> Iterator[Listing]:
        if self._context is None:
            raise SearchError("FacebookMarketplace must be used as a context manager")
        seen_ids: set[str] = set()
        page = self._context.new_page()
        page.set_default_timeout(self._page_timeout_ms)
        try:
            for phrase in item.search_phrases:
                if len(seen_ids) >= self._max_results:
                    break
                self._pause.wait()
                url = build_search_url(phrase, item, self._config)
                log.info("facebook search %r", phrase)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_selector(_ITEM_ANCHOR, timeout=self._page_timeout_ms)
                    for _ in range(self._scrolls):
                        page.mouse.wheel(0, 4000)
                        page.wait_for_timeout(1200)
                except Exception as exc:  # noqa: BLE001 - skip a failed phrase, keep going
                    log.warning("facebook search for %r failed: %s", phrase, exc)
                    continue
                yield from self._collect(page, seen_ids)
        finally:
            page.close()

    def _collect(self, page: Any, seen_ids: set[str]) -> Iterator[Listing]:
        anchors = page.query_selector_all(_ITEM_ANCHOR)
        for anchor in anchors:
            if len(seen_ids) >= self._max_results:
                return
            href = anchor.get_attribute("href") or ""
            item_id = _extract_item_id(href)
            if item_id is None or item_id in seen_ids:
                continue
            try:
                text = anchor.inner_text()
            except Exception:  # noqa: BLE001 - element may have detached
                continue
            price, title, location = _parse_card_text(text)
            log.debug(
                "card id=%s price=%s title=%r location=%r raw=%r",
                item_id,
                price,
                title,
                location,
                text.replace("\n", " | "),
            )
            if not title:
                continue
            seen_ids.add(item_id)
            yield Listing(
                id=item_id,
                marketplace=self.name,
                title=title,
                url=f"https://www.facebook.com/marketplace/item/{item_id}/",
                price=price,
                currency=_detect_currency(text),
                location=location,
                description=text.strip(),
            )

    def fetch_details(self, listing: Listing) -> Listing:
        if self._context is None:  # not in a `with` block; nothing we can do
            return listing
        page = self._context.new_page()
        page.set_default_timeout(self._page_timeout_ms)
        try:
            self._pause.wait()
            page.goto(listing.url, wait_until="domcontentloaded")
            try:  # let the SPA render the description; FB rarely goes fully idle
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001 - extract whatever rendered anyway
                pass
            _expand_description(page)
            text = _extract_detail_text(page)
            images = _extract_image_urls(page)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment, keep the card text
            log.warning("detail fetch failed for %s: %s", listing.id, exc)
            return listing
        finally:
            page.close()
        updates: dict[str, Any] = {}
        if images:
            log.debug("detail id=%s images=%d first=%r", listing.id, len(images), images[0][:120])
            updates["image_urls"] = images
        # Only replace when the detail page genuinely adds text over the card.
        if len(text) > len(listing.description):
            log.debug("detail id=%s text[%d]=%r", listing.id, len(text), text[:300])
            updates["description"] = text
        else:
            log.debug("detail id=%s no richer text (len=%d)", listing.id, len(text))
        return replace(listing, **updates) if updates else listing


def capture_session(
    config: MarketplaceConfig,
    *,
    wait_for_login: Callable[[], None],
    headless: bool = False,
) -> Path:
    """Open a headful browser for a one-time manual login and save the session.

    ``wait_for_login`` blocks until the operator has finished logging in (the CLI
    passes a function that waits for the user to press Enter).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional browser dep
        raise SearchError(
            "playwright is not available; install it and run 'playwright install chromium'"
        ) from exc

    session_path = _resolve_session_path(config)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        wait_for_login()
        context.storage_state(path=str(session_path))
        context.close()
        browser.close()
    return session_path
