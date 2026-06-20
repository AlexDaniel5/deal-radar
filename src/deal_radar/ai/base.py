"""The evaluator interface. Claude-backed implementation arrives in Phase 1."""

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
