"""Tests for the shared BackgroundJob worker used by the scanner, sender, and wizard."""

from __future__ import annotations

import threading
import time

from deal_radar.web.worker import BackgroundJob


def _wait(pred: object, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


def test_runs_job_and_reports_busy() -> None:
    started = threading.Event()
    job = BackgroundJob("test")

    assert job.start(lambda stop: (started.set(), stop.wait())) is True
    assert started.wait(1.0)
    assert job.is_busy()
    job.stop()
    assert _wait(lambda: not job.is_busy())


def test_rejects_a_second_job_while_busy() -> None:
    job = BackgroundJob("test")
    job.start(lambda stop: stop.wait())
    assert _wait(job.is_busy)
    assert job.start(lambda stop: None) is False
    job.stop()
    assert _wait(lambda: not job.is_busy())


def test_exception_becomes_status_not_a_crash() -> None:
    job = BackgroundJob("test")

    def boom(stop: threading.Event) -> None:
        raise RuntimeError("kaboom")

    job.start(boom)
    assert _wait(lambda: not job.is_busy())
    assert job.error is not None
    assert "kaboom" in job.error
    assert "RuntimeError" in job.error


def test_error_clears_on_next_start() -> None:
    job = BackgroundJob("test")
    job.start(lambda stop: (_ for _ in ()).throw(RuntimeError("first")))
    # Wait for the slot to free, not just for the error: the error is recorded
    # before the thread finishes unwinding, and start() refuses while busy.
    assert _wait(lambda: not job.is_busy())
    assert job.error is not None
    assert job.start(lambda stop: None) is True
    assert _wait(lambda: not job.is_busy())
    assert job.error is None


def test_on_finish_runs_after_success_and_after_failure() -> None:
    for fn in (lambda stop: None, lambda stop: (_ for _ in ()).throw(RuntimeError("x"))):
        finished = threading.Event()
        job = BackgroundJob("test")
        job.start(fn, on_finish=finished.set)
        assert finished.wait(2.0), "on_finish must run on both paths"


def test_slot_stays_busy_until_the_finish_hook_completes() -> None:
    """Otherwise a new job can start and have its state clobbered by the old hook."""
    job = BackgroundJob("test")
    in_hook = threading.Event()
    release = threading.Event()
    busy_during_hook: list[bool] = []

    def hook() -> None:
        in_hook.set()
        release.wait(2.0)
        busy_during_hook.append(job.is_busy())

    job.start(lambda stop: None, on_finish=hook)
    assert in_hook.wait(2.0)
    assert job.is_busy(), "must still report busy while cleanup runs"
    assert job.start(lambda stop: None) is False, "must refuse a new job during cleanup"
    release.set()
    assert _wait(lambda: not job.is_busy())
    assert busy_during_hook == [True]


def test_a_failing_finish_hook_does_not_break_the_job() -> None:
    job = BackgroundJob("test")

    def bad_hook() -> None:
        raise RuntimeError("hook exploded")

    assert job.start(lambda stop: None, on_finish=bad_hook) is True
    assert _wait(lambda: not job.is_busy())
    # The slot is still free, so the next job can start.
    assert job.start(lambda stop: None) is True


def test_each_run_gets_a_fresh_stop_event() -> None:
    job = BackgroundJob("test")
    job.start(lambda stop: stop.wait())
    assert _wait(job.is_busy)
    job.stop()
    assert _wait(lambda: not job.is_busy())

    # A stop() from the previous run must not immediately end the next one.
    seen: list[bool] = []
    job.start(lambda stop: seen.append(stop.is_set()) or stop.wait())
    assert _wait(lambda: bool(seen))
    assert seen[0] is False
    job.stop()
    assert _wait(lambda: not job.is_busy())


def test_is_stopping_only_while_a_stop_is_pending() -> None:
    job = BackgroundJob("test")
    release = threading.Event()
    job.start(lambda stop: release.wait())
    assert _wait(job.is_busy)
    assert job.is_stopping() is False
    job.stop()
    assert job.is_stopping() is True  # asked to stop, still running
    release.set()
    assert _wait(lambda: not job.is_busy())
    assert job.is_stopping() is False  # not running: nothing to stop
