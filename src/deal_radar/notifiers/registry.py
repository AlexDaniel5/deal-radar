"""Build concrete notifiers from validated config."""

from __future__ import annotations

from ..config.schema import NotifierConfig
from .base import Notifier
from .ntfy import NtfyNotifier

# Notifier types that actually have an adapter. The config schema accepts more
# (telegram validates fine), so the UI reads this to warn *before* a scan dies
# on the NotImplementedError below.
IMPLEMENTED_NOTIFIERS: tuple[str, ...] = ("ntfy",)


def build_notifier(config: NotifierConfig) -> Notifier:
    """Instantiate the notifier for a config entry (dispatch on ``type``)."""
    if config.type == "ntfy":
        return NtfyNotifier(config)
    # TelegramNotifier arrives in Phase 4.
    raise NotImplementedError(f"notifier type {config.type!r} is not implemented yet")
