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

# Only spend image tokens when the seller points at the photos ("specs in photos").
_IMAGE_TRIGGER = "photo"

# Per-1M-token (input, output) USD prices for usage/cost logging. Estimates only;
# unknown models fall back to logging raw token counts without a dollar figure.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}


def _build_content(ai: AIConfig, item: ItemConfig, listing: Listing) -> Any:
    """User-message content: plain text, or text plus every listing photo.

    Photos are attached only when ``analyze_images`` is on AND the description
    mentions them (sellers who write "parts listed in photos" put the specs
    there) — otherwise text alone keeps the call cheap. All photos are sent,
    as image blocks preceding the text (the order Anthropic recommends).
    """
    text = build_user_prompt(item, listing)
    if not (
        ai.analyze_images
        and listing.image_urls
        and _IMAGE_TRIGGER in listing.description.lower()
    ):
        return text
    blocks: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "url", "url": url}} for url in listing.image_urls
    ]
    blocks.append(
        {
            "type": "text",
            "text": text
            + "\n\nThe listing's photos are attached. Read exact component models "
            "(GPU, CPU, RAM, PSU) from them wherever the text is vague.",
        }
    )
    return blocks


def _log_usage(model: str, usage: object) -> None:
    """Log token usage (and an estimated cost when the model's price is known)."""
    in_tok = getattr(usage, "input_tokens", None)
    out_tok = getattr(usage, "output_tokens", None)
    if in_tok is None and out_tok is None:
        return
    price = _PRICE_PER_MTOK.get(model)
    if price is not None and in_tok is not None and out_tok is not None:
        cost = in_tok / 1e6 * price[0] + out_tok / 1e6 * price[1]
        log.info("eval usage: in=%s out=%s est_cost=$%.5f (%s)", in_tok, out_tok, cost, model)
    else:
        log.info("eval usage: in=%s out=%s (%s)", in_tok, out_tok, model)


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
        content = _build_content(self._ai, item, listing)
        with_images = isinstance(content, list)
        try:
            response = self._client.messages.parse(
                model=self._ai.model,
                max_tokens=self._ai.max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_format=Verdict,
            )
        except Exception as exc:  # noqa: BLE001 - wrap any SDK/transport failure
            raise EvalError(f"Claude evaluation failed for listing {listing.id!r}: {exc}") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            _log_usage(self._ai.model, usage)

        verdict: Verdict | None = response.parsed_output
        if verdict is None:
            raise EvalError(f"Claude returned no structured output for listing {listing.id!r}")
        return Evaluation(
            match=verdict.match,
            rating=verdict.rating,
            rationale=verdict.rationale,
            model=self._ai.model,
            images_analyzed=with_images,
        )
