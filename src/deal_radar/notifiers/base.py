"""The notifier interface. Concrete notifiers (ntfy, telegram) arrive in Phase 1."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..models import NotificationEvent


@runtime_checkable
class Notifier(Protocol):
    """Delivers a notification for one or more matched listings."""

    type: str

    def notify(self, event: NotificationEvent) -> None:
        """Send a single ``event``; raise :class:`~deal_radar.errors.NotifyError` on failure."""
        ...

    def notify_digest(self, item_name: str, events: Sequence[NotificationEvent]) -> None:
        """Send one notification summarizing the best ``events`` (already ranked).

        This is what the scan pipeline uses: rather than one alert per match, a
        scan collects every qualifying listing, ranks them, and sends the top N
        as a single digest. Raise :class:`~deal_radar.errors.NotifyError` on failure.
        """
        ...
