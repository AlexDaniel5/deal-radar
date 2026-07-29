"""Turns pipeline progress events into something the status endpoint can serve.

A scan is slow by design — page loads are paced ~25s apart so deal-radar browses
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
