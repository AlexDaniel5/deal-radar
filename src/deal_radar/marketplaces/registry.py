"""Build concrete marketplace adapters from config."""

from __future__ import annotations

from ..config.schema import MarketplaceConfig
from .base import Marketplace
from .facebook import FacebookMarketplace


def build_marketplace(
    name: str,
    config: MarketplaceConfig,
    *,
    headless: bool = True,
    max_results: int = 40,
) -> Marketplace:
    """Instantiate the adapter for a marketplace name."""
    if name == "facebook":
        return FacebookMarketplace(config, headless=headless, max_results=max_results)
    raise NotImplementedError(f"marketplace {name!r} is not implemented yet")
