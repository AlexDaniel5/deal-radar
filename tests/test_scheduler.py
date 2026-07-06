"""Tests for the polling loop (with a fake scan and a fake clock)."""

from __future__ import annotations

import random

from deal_radar.config.schema import ScheduleConfig
from deal_radar.scheduler import next_delay, run_loop


def _sched(**kw: int) -> ScheduleConfig:
    base = {"poll_interval_seconds": 1800, "jitter_seconds": 600}
    base.update(kw)
    return ScheduleConfig(**base)


def test_next_delay_within_jitter_band() -> None:
    sched = _sched(poll_interval_seconds=1800, jitter_seconds=600)
    rng = random.Random(0)
    for _ in range(100):
        d = next_delay(sched, rng)
        assert 1200.0 <= d <= 2400.0


def test_next_delay_no_jitter_is_exact() -> None:
    sched = _sched(poll_interval_seconds=900, jitter_seconds=0)
    assert next_delay(sched, random.Random(1)) == 900.0


def test_next_delay_never_negative() -> None:
    # Jitter wider than the interval must still clamp at 0.
    sched = _sched(poll_interval_seconds=300, jitter_seconds=600)
    rng = random.Random(2)
    assert all(next_delay(sched, rng) >= 0.0 for _ in range(200))


def test_run_loop_runs_max_cycles_and_sleeps_between() -> None:
    calls = []
    sleeps: list[float] = []
    run_loop(
        scan=lambda: calls.append(1),
        schedule=_sched(jitter_seconds=0),
        max_cycles=3,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert len(calls) == 3
    # Sleeps happen between cycles, not after the final one.
    assert sleeps == [1800.0, 1800.0]


def test_run_loop_single_cycle_does_not_sleep() -> None:
    sleeps: list[float] = []
    cycles = run_loop(
        scan=lambda: None,
        schedule=_sched(),
        max_cycles=1,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert cycles == 1
    assert sleeps == []


def test_run_loop_backs_off_on_failure_then_recovers() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def scan() -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:  # first two cycles fail, then succeed
            raise RuntimeError("boom")

    run_loop(
        scan=scan,
        schedule=_sched(poll_interval_seconds=1800, jitter_seconds=0),
        max_cycles=3,
        backoff_initial_seconds=30.0,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    # Cycle 1 fail -> 30s, cycle 2 fail -> 60s (exponential), then no sleep after final cycle 3.
    assert sleeps == [30.0, 60.0]


def test_run_loop_should_stop_before_first_cycle() -> None:
    calls: list[int] = []
    cycles = run_loop(
        scan=lambda: calls.append(1),
        schedule=_sched(),
        sleep=lambda _d: None,
        rng=random.Random(0),
        should_stop=lambda: True,  # stop immediately
    )
    assert cycles == 0
    assert calls == []


def test_run_loop_should_stop_after_one_cycle() -> None:
    calls: list[int] = []

    def stop() -> bool:
        return len(calls) >= 1  # stop once the first scan has run

    cycles = run_loop(
        scan=lambda: calls.append(1),
        schedule=_sched(jitter_seconds=0),
        sleep=lambda _d: None,
        rng=random.Random(0),
        should_stop=stop,
    )
    assert cycles == 1
    assert len(calls) == 1


def test_run_loop_backoff_caps() -> None:
    sleeps: list[float] = []

    def always_fail() -> None:
        raise RuntimeError("boom")

    run_loop(
        scan=always_fail,
        schedule=_sched(),
        max_cycles=10,
        backoff_initial_seconds=30.0,
        backoff_max_seconds=120.0,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    # 30, 60, 120, then capped at 120 for the rest (9 sleeps; none after the 10th cycle).
    assert sleeps == [30.0, 60.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0]
    assert max(sleeps) == 120.0
