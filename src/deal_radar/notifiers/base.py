"""The notifier interface. Concrete notifiers (ntfy, telegram) arrive in Phase 1."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import NotificationEvent


@runtime_checkable
class Notifier(Protocol):
    """Delivers a notification for a matched listing."""

    type: str

    def notify(self, event: NotificationEvent) -> None:
        """Send ``event``; raise :class:`~deal_radar.errors.NotifyError` on failure."""
        ...
