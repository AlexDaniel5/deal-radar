"""AI interfaces: the listing evaluator and the seller-message composer."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config.schema import ItemConfig
from ..models import Evaluation, Listing


@runtime_checkable
class Evaluator(Protocol):
    """Judges whether a listing matches an item and how good a deal it is."""

    def evaluate(self, item: ItemConfig, listing: Listing) -> Evaluation:
        """Return an :class:`Evaluation` (match / 1-5 rating / one-line rationale)."""
        ...


@runtime_checkable
class Composer(Protocol):
    """Writes a short first message to a seller for a matched listing."""

    def compose(self, item: ItemConfig, listing: Listing, offer_price: int | None) -> str:
        """Return the message text; ``offer_price`` of None means availability-only."""
        ...
