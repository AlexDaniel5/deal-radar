"""ntfy notifier (https://ntfy.sh)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from ..config.schema import NtfyNotifierConfig
from ..errors import NotifyError
from ..logging import get_logger
from ..models import NotificationEvent

log = get_logger("notifier.ntfy")


def _price(listing: Any) -> str:
    if listing.price is not None:
        return f"{listing.price:.0f} {listing.currency}"
    return "price unknown"


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
        payload: dict[str, Any] = {
            "topic": self._config.topic,
            "title": f"[{evaluation.rating}/5] {event.item_name}",
            "message": (
                f"{listing.title}\n"
                f"{_price(listing)} - {listing.location or 'location unknown'}\n"
                f"{evaluation.rationale}"
                + ("\nReply draft ready in the web UI" if event.draft_pending else "")
            ),
            "click": listing.url,
            # "camera" renders as a 📷 icon: the AI looked through the photos.
            "tags": ["moneybag", "camera"] if evaluation.images_analyzed else ["moneybag"],
        }
        self._publish(payload, note=event.item_name)

    def notify_digest(self, item_name: str, events: Sequence[NotificationEvent]) -> None:
        """Send the ranked top matches as a single "top N" notification.

        Listings are already ordered best-first by the caller. Each is one
        numbered block (rank, rating, price, location, title, and its URL so the
        link is tappable); ntfy's ``click`` opens the #1 pick.
        """
        if not events:
            return
        if len(events) == 1:
            # A single match reads better as the normal one-listing alert.
            self.notify(events[0])
            return

        blocks: list[str] = []
        for rank, event in enumerate(events, start=1):
            listing = event.listing
            blocks.append(
                f"{rank}. [{event.evaluation.rating}/5] {_price(listing)}"
                f" - {listing.location or 'location unknown'}\n"
                f"{listing.title}\n{listing.url}"
            )
        message = "\n\n".join(blocks)
        if any(e.draft_pending for e in events):
            message += "\n\nReply drafts ready in the web UI"

        payload: dict[str, Any] = {
            "topic": self._config.topic,
            "title": f"Top {len(events)} {item_name} deals",
            "message": message,
            "click": events[0].listing.url,  # the #1 ranked pick
            "tags": (
                ["moneybag", "camera"]
                if any(e.evaluation.images_analyzed for e in events)
                else ["moneybag"]
            ),
        }
        self._publish(payload, note=f"{item_name} (top {len(events)})")

    def _publish(self, payload: dict[str, Any], *, note: str) -> None:
        if self._config.priority is not None:
            payload["priority"] = self._config.priority
        try:
            response = self._client.post(self._config.server, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy publish failed: {exc}") from exc
        log.info("notified ntfy topic %s for %s", self._config.topic, note)
