"""Runs the scanner in a background thread so the web server can control it.

The scanner uses Playwright's sync API, which must NOT run inside an asyncio
event loop — so it runs in a dedicated worker thread here, separate from
uvicorn's loop (see :class:`~deal_radar.web.worker.BackgroundJob`, which owns
the threading). The controller is deliberately generic: it takes two job
callables (loop / once), each receiving a stop Event, so it can be unit-tested
with fake jobs and no browser.
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger
from .worker import BackgroundJob, Job

log = get_logger("web.controller")

__all__ = ["Job", "ScannerController"]


class ScannerController:
    """Owns at most one scanner worker thread at a time."""

    def __init__(self, run_loop_job: Job, run_once_job: Job) -> None:
        self._jobs: dict[str, Job] = {"loop": run_loop_job, "once": run_once_job}
        self._job = BackgroundJob("deal-radar-scanner")
        self._mode: str | None = None

    def start(self, mode: str) -> bool:
        """Start the given job in a worker thread. Returns False if already running."""
        if mode not in self._jobs:
            raise ValueError(f"unknown scanner mode: {mode!r}")
        started = self._job.start(self._jobs[mode], on_finish=self._clear_mode)
        if started:
            # Only claim the mode on success, so a rejected start can't relabel the
            # job that is already running. A very short job may clear this again
            # first, which is harmless: status() reports mode only while running.
            self._mode = mode
        return started

    def start_with(self, job: Job, *, mode: str) -> bool:
        """Run a one-off job (e.g. the free test scan) under the same single-slot rule."""
        started = self._job.start(job, on_finish=self._clear_mode)
        if started:
            self._mode = mode
        return started

    def _clear_mode(self) -> None:
        self._mode = None

    def stop(self) -> None:
        """Request a cooperative stop (the job checks the Event at safe points)."""
        self._job.stop()

    def is_running(self) -> bool:
        return self._job.is_busy()

    def status(self) -> dict[str, Any]:
        running = self.is_running()
        return {
            "running": running,
            "mode": self._mode if running else None,
            "stopping": self._job.is_stopping(),
            "error": self._job.error,
        }
