"""ntfy notifier (https://ntfy.sh)."""

from __future__ import annotations

from typing import Any

import httpx

from ..config.schema import NtfyNotifierConfig
from ..errors import NotifyError
from ..logging import get_logger
from ..models import NotificationEvent

log = get_logger("notifier.ntfy")


class NtfyNotifier:
    """Publishes a match to an ntfy topic via the JSON publishing API.

    Posting JSON to the server root (rather than per-topic headers) avoids
    HTTP-header encoding issues with non-ASCII titles/messages.
    """

    type = "ntfy"

    def __init__(self, config: NtfyNotifierConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client if client is not None else httpx.Client(timeout=15.0)

    def notify(self, event: NotificationEvent) -> None:
        listing = event.listing
        evaluation = event.evaluation
        if listing.price is not None:
            price = f"{listing.price:.0f} {listing.currency}"
        else:
            price = "price unknown"

        payload: dict[str, Any] = {
            "topic": self._config.topic,
            "title": f"[{evaluation.rating}/5] {event.item_name}",
            "message": (
                f"{listing.title}\n"
                f"{price} - {listing.location or 'location unknown'}\n"
                f"{evaluation.rationale}"
            ),
            "click": listing.url,
            "tags": ["moneybag"],
        }
        if self._config.priority is not None:
            payload["priority"] = self._config.priority

        try:
            response = self._client.post(self._config.server, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy publish failed: {exc}") from exc
        log.info("notified ntfy topic %s for %s", self._config.topic, event.item_name)
