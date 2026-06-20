"""The marketplace interface. Concrete adapters arrive in Phase 1."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config.schema import ItemConfig, MarketplaceConfig
from ..models import Listing


@dataclass(slots=True)
class SearchContext:
    """Per-run context handed to a marketplace adapter."""

    config: MarketplaceConfig
    dry_run: bool = False


@runtime_checkable
class Marketplace(Protocol):
    """Searches a marketplace and yields parsed listings for an item."""

    name: str

    def search(self, item: ItemConfig, ctx: SearchContext) -> Iterator[Listing]:
        """Yield listings matching ``item``'s search phrases / filters."""
        ...
