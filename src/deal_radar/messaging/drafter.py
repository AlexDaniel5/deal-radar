"""Composes and stores a pending seller message for a matched listing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

from ..ai.base import Composer
from ..config.schema import AppConfig, ItemConfig, MessagingConfig
from ..logging import get_logger
from ..models import Evaluation, Listing
from .offer import compute_offer
from .store import SqliteDraftStore

log = get_logger("messaging.drafter")


@runtime_checkable
class Drafter(Protocol):
    """Creates a pending seller-message draft for a matched listing."""

    def draft(self, item: ItemConfig, listing: Listing, evaluation: Evaluation) -> None: ...


class MessageDrafter:
    """Computes the offer in Python, lets Claude write the wording, stores the draft."""

    def __init__(
        self, messaging: MessagingConfig, composer: Composer, store: SqliteDraftStore
    ) -> None:
        self._messaging = messaging
        self._composer = composer
        self._store = store

    def draft(self, item: ItemConfig, listing: Listing, evaluation: Evaluation) -> None:
        offer = None
        if item.effective_negotiate(self._messaging):
            offer = compute_offer(listing.price, item.effective_offer_percent(self._messaging))
        text = self._composer.compose(item, listing, offer)
        draft_id = self._store.add_draft(
            item_name=item.name, listing=listing, message=text, offer_price=offer
        )
        log.info(
            "draft #%d ready for %s: %s (offer=%s)",
            draft_id,
            item.name,
            listing.title,
            offer if offer is not None else "asking",
        )


@contextmanager
def open_drafter(cfg: AppConfig) -> Iterator[MessageDrafter | None]:
    """Yield a ready :class:`MessageDrafter` (owning its store) when messaging is
    enabled in ``cfg``, else None — so call sites stay a one-line ``with``."""
    if not cfg.messaging.enabled:
        yield None
        return
    from .. import paths  # local: keep offline paths import-light
    from ..ai.composer import ClaudeComposer

    with SqliteDraftStore(paths.db_path()) as store:
        yield MessageDrafter(cfg.messaging, ClaudeComposer(cfg.ai), store)
