"""The polling loop: repeat a scan pass on an interval, with jitter + backoff.

The loop itself is pure orchestration and takes the scan as an injected callable,
so it can be exercised in tests with a fake scan and a fake clock — no browser,
no network, no real sleeping.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from .config.schema import ScheduleConfig
from .logging import get_logger

log = get_logger("scheduler")


def next_delay(schedule: ScheduleConfig, rng: random.Random) -> float:
    """Seconds to wait after a successful cycle: poll interval +/- jitter (>= 0)."""
    base = float(schedule.poll_interval_seconds)
    jitter = float(schedule.jitter_seconds)
    if jitter > 0:
        base += rng.uniform(-jitter, jitter)
    return max(0.0, base)


def run_loop(
    *,
    scan: Callable[[], None],
    schedule: ScheduleConfig,
    max_cycles: int | None = None,
    backoff_initial_seconds: float = 30.0,
    backoff_max_seconds: float = 900.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> int:
    """Run ``scan`` repeatedly until interrupted (or ``max_cycles`` is reached).

    Between successful cycles it sleeps ``poll_interval +/- jitter``. If a whole
    cycle raises, it logs and backs off with exponential delay (capped), resetting
    the backoff once a cycle succeeds again. Returns the number of cycles run.
    """
    rng = rng if rng is not None else random.Random()
    cycle = 0
    consecutive_failures = 0

    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        log.info("scan cycle %d starting", cycle)
        try:
            scan()
        except Exception as exc:  # noqa: BLE001 - a bad cycle backs off, never kills the loop
            consecutive_failures += 1
            delay = min(
                backoff_max_seconds,
                backoff_initial_seconds * (2 ** (consecutive_failures - 1)),
            )
            log.warning(
                "scan cycle %d failed (%d in a row): %s; backing off %.0fs",
                cycle,
                consecutive_failures,
                exc,
                delay,
            )
        else:
            consecutive_failures = 0
            delay = next_delay(schedule, rng)
            log.info("scan cycle %d done; next in %.0fs", cycle, delay)

        if max_cycles is not None and cycle >= max_cycles:
            break
        sleep(delay)

    return cycle
