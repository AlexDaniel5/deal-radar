"""Turns pipeline progress events into something the status endpoint can serve.

A scan is slow by design - page loads are paced ~25s apart so deal-radar browses
at a human rate, and every candidate costs an extra detail-page load. Twenty-five
candidates is therefore a quarter of an hour or more, during which the old UI
showed a green dot and nothing else. This holds the latest event, and estimates
how much longer there is to go from the pacing the config actually uses.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..pipeline import ProgressEvent

# What the user reads for each pipeline phase.
_PHRASES = {
    "searching": "Searching the marketplace",
    "found": "Reading search results",
    "fetching": "Opening a listing",
    "evaluating": "Asking the AI about a listing",
    "checked": "Checked a listing",
    "notifying": "Sending your alert",
    "done": "Finishing up",
}


class ProgressTracker:
    """Thread-safe holder for the most recent progress event.

    Written from the scanner's worker thread, read from the event loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event: ProgressEvent | None = None
        self._started: float | None = None
        self._seconds_per_listing = 0.0
        self._checked_at_start = 0
        # Between scans of a "Keep watching" loop: how the last one went, and
        # when the next is due - so a quiet loop doesn't look stuck.
        self._last: dict[str, Any] | None = None
        self._next_at: float | None = None

    def start(self, *, seconds_per_listing: float) -> None:
        """Begin a scan. ``seconds_per_listing`` comes from the config's pacing."""
        with self._lock:
            self._event = None
            self._started = time.time()
            self._seconds_per_listing = max(1.0, seconds_per_listing)
            self._checked_at_start = 0

    def record(self, event: ProgressEvent) -> None:
        with self._lock:
            self._event = event

    def clear(self) -> None:
        with self._lock:
            self._event = None
            self._started = None

    def finish(
        self,
        *,
        found: int,
        already_seen: int,
        evaluated: int,
        matched: int,
        super_alerts: int,
        errors: int,
    ) -> None:
        """Record a completed scan's totals (and end its live progress)."""
        with self._lock:
            self._event = None
            self._started = None
            self._last = {
                "finished_at": time.time(),
                "found": found,
                "already_seen": already_seen,
                "evaluated": evaluated,
                "matched": matched,
                "super_alerts": super_alerts,
                "errors": errors,
                # Every listing the searches returned had been checked before.
                "caught_up": found > 0 and already_seen == found,
            }

    def waiting(self, seconds: float | None) -> None:
        """The loop is sleeping ``seconds`` until its next scan (None: not waiting)."""
        with self._lock:
            self._next_at = time.time() + seconds if seconds is not None else None

    def reset(self) -> None:
        """Forget everything - the scanner has stopped."""
        with self._lock:
            self._event = None
            self._started = None
            self._last = None
            self._next_at = None

    def between_scans(self) -> dict[str, Any] | None:
        """The last scan's outcome and the wait until the next, or None mid-scan/idle."""
        with self._lock:
            if self._event is not None or self._last is None:
                return None
            last, next_at = dict(self._last), self._next_at
        last["next_scan_seconds"] = max(0, int(next_at - time.time())) if next_at else None
        return last

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            event, started = self._event, self._started
            per_listing = self._seconds_per_listing
        if event is None:
            return None
        elapsed = time.time() - started if started else 0.0
        # Prefer the rate actually observed; fall back to the configured pacing
        # before enough listings have been checked to measure anything.
        if event.checked >= 2 and elapsed > 0:
            per_listing = elapsed / event.checked
        remaining = None
        if event.of:
            left = max(0, event.of - event.checked)
            # Items still to come are only counted once we know how long one takes.
            items_left = max(0, event.item_count - event.item_index)
            remaining = int((left + items_left * event.of) * per_listing)
        return {
            "phase": event.phase,
            "message": _PHRASES.get(event.phase, event.phase),
            "item": event.item,
            "item_index": event.item_index,
            "item_count": event.item_count,
            "checked": event.checked,
            "of": event.of,
            "found": event.found,
            "matched": event.matched,
            "title": event.title,
            "elapsed_seconds": int(elapsed),
            "eta_seconds": remaining,
        }


def humanize_wait(seconds: int | None) -> str:
    """'in about 25 minutes' for the gap before the next scan."""
    if seconds is None:
        return ""
    if seconds < 90:
        return "in less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    return f"in about {seconds / 3600:.1f} hours"


def humanize_eta(seconds: int | None) -> str:
    """A deliberately rough estimate, phrased so it doesn't read as a promise."""
    if seconds is None:
        return ""
    if seconds < 90:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''} left"
    hours = seconds / 3600
    return f"about {hours:.1f} hours left"


#: Process-wide tracker: the runner writes it, the status endpoint reads it.
TRACKER = ProgressTracker()
