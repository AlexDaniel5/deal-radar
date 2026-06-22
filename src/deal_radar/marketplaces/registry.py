"""Build concrete marketplace adapters from config."""

from __future__ import annotations

from ..config.schema import MarketplaceConfig
from ..ratelimit import RateLimiter
from .base import Marketplace
from .facebook import FacebookMarketplace


def build_marketplace(
    name: str,
    config: MarketplaceConfig,
    *,
    headless: bool = True,
    max_results: int = 40,
    pause: RateLimiter | None = None,
) -> Marketplace:
    """Instantiate the adapter for a marketplace name.

    ``pause`` paces successive page loads inside the adapter (politeness); when
    omitted the adapter uses its own conservative default.
    """
    if name == "facebook":
        return FacebookMarketplace(
            config, headless=headless, max_results=max_results, pause=pause
        )
    raise NotImplementedError(f"marketplace {name!r} is not implemented yet")
