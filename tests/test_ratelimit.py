"""Tests for the rate limiter (clock/sleep injected)."""

from __future__ import annotations

import random

from deal_radar.ratelimit import RateLimiter


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _limiter(clock: _Clock, minimum: float, jitter: float = 0.0) -> RateLimiter:
    return RateLimiter(
        minimum, jitter, sleep=clock.sleep, monotonic=clock.monotonic, rng=random.Random(0)
    )


def test_first_call_does_not_wait() -> None:
    clock = _Clock()
    assert _limiter(clock, 10).wait() == 0.0
    assert clock.slept == []


def test_enforces_minimum_interval() -> None:
    clock = _Clock()
    rl = _limiter(clock, 10)
    rl.wait()  # establish baseline at t=0
    slept = rl.wait()  # no time has passed
    assert slept == 10
    assert clock.slept == [10]


def test_no_wait_when_enough_elapsed() -> None:
    clock = _Clock()
    rl = _limiter(clock, 10)
    rl.wait()
    clock.t = 20.0
    assert rl.wait() == 0.0


def test_jitter_within_bounds() -> None:
    clock = _Clock()
    rl = _limiter(clock, 10, jitter=4)
    rl.wait()
    slept = rl.wait()
    assert 10.0 <= slept <= 14.0
