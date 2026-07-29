"""Model prices, cost estimates, and a running tally of what evaluations cost.

Every AI evaluation is a real paid API call, so the web UI needs two things the
CLI never did: a cost *estimate* to show before the user clicks a scan button,
and a *measured* running total to show while the scan is going. Both live here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..config.schema import AIConfig

# Per-1M-token (input, output) USD prices. Estimates only; unknown models fall
# back to logging raw token counts without a dollar figure.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}


@dataclass(frozen=True)
class ModelChoice:
    """A model we're willing to offer in the settings picker."""

    id: str
    label: str
    note: str


# Only models that support the structured-output path the evaluator relies on
# (``client.messages.parse(..., output_format=Verdict)``). Offering anything
# else would hand the user a config that validates fine and then fails on the
# first evaluation — notably ``claude-sonnet-4-6``, which is priced above but
# is NOT on the structured-outputs list.
SUPPORTED_MODELS: tuple[ModelChoice, ...] = (
    ModelChoice("claude-haiku-4-5", "Haiku 4.5", "Recommended — cheapest and fastest"),
    ModelChoice("claude-sonnet-5", "Sonnet 5", "Smarter, about 3× the cost"),
    ModelChoice("claude-opus-4-8", "Opus 4.8", "Most capable, about 5× the cost"),
)

# Nominal token counts for a single evaluation, used only for the pre-click
# estimate. Calibrated against the ~$0.002/eval figure observed live on
# claude-haiku-4-5 with detail-page fetching on.
_NOMINAL_INPUT_TOKENS = 1200
_NOMINAL_OUTPUT_TOKENS = 150
_NOMINAL_TOKENS_PER_IMAGE = 1600


def price_for(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens, or None if we don't know this model."""
    return PRICE_PER_MTOK.get(model)


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Actual USD cost of one call, or None for an unknown model."""
    price = price_for(model)
    if price is None:
        return None
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]


def estimate_eval_cost(ai: AIConfig) -> float | None:
    """Rough USD cost of one evaluation under this config, or None if unknown.

    Returning None matters: the UI must say "cost unknown for this model"
    rather than quoting a confident number it made up.
    """
    input_tokens = _NOMINAL_INPUT_TOKENS
    if ai.analyze_images:
        input_tokens += ai.max_images * _NOMINAL_TOKENS_PER_IMAGE
    return cost_of(ai.model, input_tokens, _NOMINAL_OUTPUT_TOKENS)


class EvalMeter:
    """Thread-safe tally of evaluations and their measured cost.

    The evaluator runs on a worker thread while the web server reads these
    counters from the event loop, hence the lock. ``scan_*`` counters are reset
    at the start of each scan; ``total_*`` accumulate for the process lifetime.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scan = _Tally()
        self._total = _Tally()

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """Record one evaluation; returns its cost, or None for an unknown model."""
        cost = cost_of(model, input_tokens, output_tokens)
        with self._lock:
            for tally in (self._scan, self._total):
                tally.evals += 1
                tally.input_tokens += input_tokens
                tally.output_tokens += output_tokens
                if cost is None:
                    tally.cost_known = False
                else:
                    tally.cost += cost
        return cost

    def start_scan(self) -> None:
        """Reset the per-scan counters."""
        with self._lock:
            self._scan = _Tally()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"scan": self._scan.as_dict(), "total": self._total.as_dict()}


@dataclass
class _Tally:
    evals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "evals": self.evals,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": round(self.cost, 5),
            "cost_known": self.cost_known,
        }


# Process-wide meter. The evaluator increments it; the web layer reads it.
METER = EvalMeter()
