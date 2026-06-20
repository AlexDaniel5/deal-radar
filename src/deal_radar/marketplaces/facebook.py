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
from collections.abc import Callable, Iterator
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
    """Best-effort split of a listing card's text into (price, title, location)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, "", None

    price: float | None = None
    price_idx: int | None = None
    for i, ln in enumerate(lines):
        if "$" in ln or "free" in ln.lower():
            parsed = _parse_price(ln)
            if parsed is not None:
                price, price_idx = parsed, i
                break

    remaining = [ln for i, ln in enumerate(lines) if i != price_idx]
    title = remaining[0] if remaining else lines[0]
    location = remaining[-1] if len(remaining) > 1 else None
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
            if not title:
                continue
            seen_ids.add(item_id)
            yield Listing(
                id=item_id,
                marketplace=self.name,
                title=title,
                url=f"https://www.facebook.com/marketplace/item/{item_id}/",
                price=price,
                currency="USD",
                location=location,
                description=text.strip(),
            )


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
