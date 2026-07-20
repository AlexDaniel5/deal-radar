"""Claude-backed seller-message composer (Anthropic Python SDK)."""

from __future__ import annotations

import os
from typing import Any

from ..config.schema import AIConfig, ItemConfig
from ..errors import EvalError
from ..logging import get_logger
from ..models import Listing
from .claude import _log_usage
from .prompt import COMPOSE_SYSTEM, DraftMessage, build_compose_prompt

log = get_logger("ai.composer")


class ClaudeComposer:
    """Writes seller messages via the Claude Messages API with structured output.

    The offer price is computed deterministically by the caller; Claude only
    writes the wording. The Anthropic client is created from
    ``AIConfig.api_key_env`` unless one is injected (used by tests).
    """

    def __init__(self, ai: AIConfig, *, client: Any | None = None) -> None:
        self._ai = ai
        if client is not None:
            self._client = client
            return
        import anthropic  # lazy: keep import cost out of offline paths

        key = os.environ.get(ai.api_key_env)
        if not key:
            raise EvalError(f"missing API key: environment variable {ai.api_key_env!r} is not set")
        self._client = anthropic.Anthropic(api_key=key)

    def compose(self, item: ItemConfig, listing: Listing, offer_price: int | None) -> str:
        prompt = build_compose_prompt(item, listing, offer_price)
        try:
            response = self._client.messages.parse(
                model=self._ai.model,
                max_tokens=self._ai.max_tokens,
                system=COMPOSE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=DraftMessage,
            )
        except Exception as exc:  # noqa: BLE001 - wrap any SDK/transport failure
            raise EvalError(
                f"Claude message composition failed for listing {listing.id!r}: {exc}"
            ) from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            _log_usage(self._ai.model, usage)

        draft: DraftMessage | None = response.parsed_output
        if draft is None or not draft.message.strip():
            raise EvalError(f"Claude returned no message text for listing {listing.id!r}")
        return draft.message.strip()
