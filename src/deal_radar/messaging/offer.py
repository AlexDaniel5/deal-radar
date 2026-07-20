"""Deterministic opening-offer math (the AI only writes wording, never prices)."""

from __future__ import annotations

import math


def compute_offer(price: float | None, percent: int) -> int | None:
    """Opening offer: ``percent`` of asking, half-up to the nearest $5, never above asking.

    Returns ``None`` when no sensible offer exists (unknown/free price, or the
    offer rounds to <= 0) — callers then draft an availability-only message.
    """
    if price is None or price <= 0:
        return None
    raw = price * percent / 100
    # Half-up to the nearest $5; builtin round() half-to-even would surprise here.
    offer = int(math.floor(raw / 5 + 0.5)) * 5
    offer = min(offer, int(price))  # whole dollars, never above asking
    return offer if offer > 0 else None
