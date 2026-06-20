"""Politeness helper: minimum interval (plus optional jitter) between calls."""

from __future__ import annotations

import random
import time
from collections.abc import Callable


class RateLimiter:
    """Blocks so that successive :meth:`wait` calls are spaced out.

    Clock and sleep are injectable for testing.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        jitter_seconds: float = 0.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._min = max(0.0, float(min_interval_seconds))
        self._jitter = max(0.0, float(jitter_seconds))
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng if rng is not None else random.Random()
        self._last: float | None = None

    def wait(self) -> float:
        """Sleep if needed; return the number of seconds slept."""
        now = self._monotonic()
        if self._last is None:
            self._last = now
            return 0.0
        target = self._min + (self._rng.random() * self._jitter if self._jitter else 0.0)
        delay = target - (now - self._last)
        if delay > 0:
            self._sleep(delay)
            self._last = self._monotonic()
            return delay
        self._last = now
        return 0.0
