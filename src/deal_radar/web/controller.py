"""Runs the scanner in a background thread so the web server can control it.

The scanner uses Playwright's sync API, which must NOT run inside an asyncio
event loop — so it runs in a dedicated worker thread here, separate from
uvicorn's loop. The controller is deliberately generic: it takes two job
callables (loop / once), each receiving a stop Event, so it can be unit-tested
with fake jobs and no browser.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..logging import get_logger

log = get_logger("web.controller")

Job = Callable[[threading.Event], None]


class ScannerController:
    """Owns at most one scanner worker thread at a time."""

    def __init__(self, run_loop_job: Job, run_once_job: Job) -> None:
        self._jobs: dict[str, Job] = {"loop": run_loop_job, "once": run_once_job}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mode: str | None = None
        self._error: str | None = None

    def _run(self, job: Job) -> None:
        try:
            job(self._stop)
        except Exception as exc:  # noqa: BLE001 - surface as status, don't crash the server
            self._error = f"{type(exc).__name__}: {exc}"
            log.exception("scanner job failed")
        finally:
            with self._lock:
                self._thread = None
                self._mode = None

    def start(self, mode: str) -> bool:
        """Start the given job in a worker thread. Returns False if already running."""
        if mode not in self._jobs:
            raise ValueError(f"unknown scanner mode: {mode!r}")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self._error = None
            self._mode = mode
            thread = threading.Thread(
                target=self._run, args=(self._jobs[mode],), name="deal-radar-scanner", daemon=True
            )
            self._thread = thread
            thread.start()
            return True

    def stop(self) -> None:
        """Request a cooperative stop (the job checks the Event at safe points)."""
        self._stop.set()

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        running = self.is_running()
        return {
            "running": running,
            "mode": self._mode if running else None,
            "stopping": running and self._stop.is_set(),
            "error": self._error,
        }
