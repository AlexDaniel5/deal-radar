"""Claude-backed listing evaluator (Anthropic Python SDK)."""

from __future__ import annotations

import os
from typing import Any

from ..config.schema import AIConfig, ItemConfig
from ..errors import EvalError
from ..logging import get_logger
from ..models import Evaluation, Listing
from .prompt import SYSTEM, Verdict, build_user_prompt

log = get_logger("ai.claude")


class ClaudeEvaluator:
    """Evaluates listings via the Claude Messages API with structured output.

    The Anthropic client is created from ``AIConfig.api_key_env`` unless one is
    injected (used by tests).
    """

    def __init__(self, ai: AIConfig, *, client: Any | None = None) -> None:
        self._ai = ai
        if client is not None:
            self._client = client
            return
        import anthropic  # lazy: keep import cost out of offline paths

        key = os.environ.get(ai.api_key_env)
        if not key:
            raise EvalError(
                f"missing API key: environment variable {ai.api_key_env!r} is not set"
            )
        self._client = anthropic.Anthropic(api_key=key)

    def evaluate(self, item: ItemConfig, listing: Listing) -> Evaluation:
        try:
            response = self._client.messages.parse(
                model=self._ai.model,
                max_tokens=self._ai.max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": build_user_prompt(item, listing)}],
                output_format=Verdict,
            )
        except Exception as exc:  # noqa: BLE001 - wrap any SDK/transport failure
            raise EvalError(f"Claude evaluation failed for listing {listing.id!r}: {exc}") from exc

        verdict: Verdict | None = response.parsed_output
        if verdict is None:
            raise EvalError(f"Claude returned no structured output for listing {listing.id!r}")
        return Evaluation(
            match=verdict.match,
            rating=verdict.rating,
            rationale=verdict.rationale,
            model=self._ai.model,
        )
