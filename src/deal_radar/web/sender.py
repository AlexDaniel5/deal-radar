"""Sends approved seller-message drafts in a background worker thread.

Playwright's sync API must not run inside uvicorn's event loop, so sends run in
a dedicated thread (same reasoning as :class:`ScannerController`). At most one
send is in flight at a time; each worker opens its own draft-store connection
(sqlite connections are not shared across threads).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..errors import SendError
from ..logging import get_logger
from ..messaging.store import SqliteDraftStore
from .worker import BackgroundJob

log = get_logger("web.sender")

# (draft_row, text) -> None; raises SendError on failure.
SendFn = Callable[[dict[str, Any], str], None]


class MessageSender:
    """Owns at most one message-send worker thread at a time."""

    def __init__(self, send_fn: SendFn, store_factory: Callable[[], SqliteDraftStore]) -> None:
        self._send_fn = send_fn
        self._store_factory = store_factory
        self._job = BackgroundJob("deal-radar-sender")
        self._draft_id: int | None = None
        self._last_error: str | None = None

    def _run(self, draft: dict[str, Any], text: str) -> None:
        """Send, then record the outcome on the draft row.

        Failures become a ``failed`` draft status rather than propagating: the
        user needs to see *why* their message didn't go out, and a retry is one
        click away.
        """
        draft_id = int(draft["id"])
        try:
            self._send_fn(draft, text)
            status, error = "sent", None
        except Exception as exc:  # noqa: BLE001 - surface as draft status, don't crash
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            self._last_error = error
            log.exception("sending draft #%d failed", draft_id)
        with self._store_factory() as store:
            store.set_status(draft_id, status, error=error)

    def send(self, draft: dict[str, Any], text: str) -> bool:
        """Send an approved draft in a worker thread. Returns False if one is in flight."""
        draft_id = int(draft["id"])

        def job(_stop: threading.Event) -> None:
            self._last_error = None  # cleared here so a rejected send can't wipe it
            self._run(draft, text)

        started = self._job.start(job, on_finish=self._clear_draft)
        if started:
            self._draft_id = draft_id
        return started

    def _clear_draft(self) -> None:
        self._draft_id = None

    def is_busy(self) -> bool:
        return self._job.is_busy()

    def status(self) -> dict[str, Any]:
        busy = self.is_busy()
        return {
            "sending": busy,
            "draft_id": self._draft_id if busy else None,
            "last_error": self._last_error,
        }


def build_send_fn(config_path: str, *, headless: bool = True) -> SendFn:
    """Production send function: fresh config, fresh browser, one listing, slow pacing."""

    def _send(draft: dict[str, Any], text: str) -> None:
        from ..config.loader import load_config
        from ..marketplaces.facebook import FacebookMarketplace
        from ..ratelimit import RateLimiter

        cfg = load_config(config_path)  # reload: respect the current toggle
        if not cfg.messaging.enabled:
            raise SendError("messaging is disabled in config")
        marketplace = str(draft["marketplace"])
        mk_cfg = cfg.marketplaces.get(marketplace)
        if marketplace != "facebook" or mk_cfg is None:
            raise SendError(f"cannot send on marketplace {marketplace!r}")
        pause = RateLimiter(30.0, 15.0)  # dedicated slow pacer for sends
        with FacebookMarketplace(mk_cfg, headless=headless, pause=pause) as mk:
            mk.send_message(str(draft["url"]), text)

    return _send
