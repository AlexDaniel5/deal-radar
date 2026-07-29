"""One-at-a-time background worker threads for the web server.

Playwright's sync API must not run inside uvicorn's asyncio loop, so every
browser-driving operation the web UI offers — scanning, sending a seller
message, capturing a Facebook login, checking a saved session — runs in a
dedicated daemon thread instead.

All of those share the same skeleton: a lock, at most one live thread, a stop
:class:`threading.Event` handed to the job, and an exception captured as a
status string rather than crashing the server. :class:`BackgroundJob` is that
skeleton. Callers keep whatever state is genuinely theirs (which mode is
running, which draft is being sent) and delegate the threading here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..logging import get_logger

log = get_logger("web.worker")

Job = Callable[[threading.Event], None]


class BackgroundJob:
    """Owns at most one daemon worker thread at a time.

    ``name`` is used for the thread name and in log messages.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: str | None = None

    def _run(self, fn: Job, stop: threading.Event, on_finish: Callable[[], None] | None) -> None:
        try:
            try:
                fn(stop)
            except Exception as exc:  # noqa: BLE001 - surface as status, don't crash the server
                self._error = f"{type(exc).__name__}: {exc}"
                log.exception("%s job failed", self._name)
            finally:
                # Run cleanup *before* freeing the slot. If the slot were freed
                # first, is_busy() would report idle while this hook was still
                # running, and a caller who immediately started a new job could
                # have its state wiped by the old job's hook.
                if on_finish is not None:
                    try:
                        on_finish()
                    except Exception:  # noqa: BLE001 - cleanup must not mask the job
                        log.exception("%s finish hook failed", self._name)
        finally:
            with self._lock:
                self._thread = None

    def start(self, fn: Job, *, on_finish: Callable[[], None] | None = None) -> bool:
        """Run ``fn`` in a worker thread. Returns False if one is already in flight.

        A fresh stop Event is created per run and passed to ``fn``; ``on_finish``
        runs after the job returns or raises, once the thread slot is free.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self._error = None
            thread = threading.Thread(
                target=self._run, args=(fn, self._stop, on_finish), name=self._name, daemon=True
            )
            self._thread = thread
            thread.start()
            return True

    def stop(self) -> None:
        """Request a cooperative stop; the job decides where it is safe to honour it."""
        self._stop.set()

    def is_busy(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def is_stopping(self) -> bool:
        return self.is_busy() and self._stop.is_set()

    @property
    def error(self) -> str | None:
        """The last uncaught job exception, as ``"TypeName: message"``."""
        return self._error

    def status(self) -> dict[str, Any]:
        return {"busy": self.is_busy(), "stopping": self.is_stopping(), "error": self._error}
