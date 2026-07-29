"""Tests for model pricing, cost estimates, and the evaluation spend meter."""

from __future__ import annotations

from deal_radar.ai.pricing import (
    PRICE_PER_MTOK,
    SUPPORTED_MODELS,
    EvalMeter,
    cost_of,
    estimate_eval_cost,
    price_for,
)
from deal_radar.config.schema import AIConfig


def test_known_and_unknown_models() -> None:
    assert price_for("claude-haiku-4-5") == (1.0, 5.0)
    assert price_for("gpt-nope") is None
    assert cost_of("gpt-nope", 1000, 100) is None


def test_cost_of_uses_both_rates() -> None:
    # 1M in at $1 + 1M out at $5.
    assert cost_of("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_estimate_is_in_the_observed_ballpark() -> None:
    """The live-observed figure is ~$0.002/eval on haiku with detail fetch."""
    est = estimate_eval_cost(AIConfig(model="claude-haiku-4-5"))
    assert est is not None
    assert 0.001 < est < 0.004


def test_estimate_rises_when_photos_are_analyzed() -> None:
    text_only = estimate_eval_cost(AIConfig(model="claude-haiku-4-5"))
    with_photos = estimate_eval_cost(
        AIConfig(model="claude-haiku-4-5", analyze_images=True, max_images=3)
    )
    assert text_only is not None and with_photos is not None
    assert with_photos > text_only


def test_estimate_is_none_for_unknown_model() -> None:
    """None means the UI says 'cost unknown' rather than inventing a number."""
    assert estimate_eval_cost(AIConfig(model="some-future-model")) is None


def test_every_offered_model_is_priced() -> None:
    for choice in SUPPORTED_MODELS:
        assert choice.id in PRICE_PER_MTOK, f"{choice.id} is offered but has no price"


def test_offered_models_all_support_structured_outputs() -> None:
    """The evaluator uses messages.parse(output_format=...).

    Offering a model without structured-output support would hand the user a
    config that validates and then fails on the first evaluation. Notably
    claude-sonnet-4-6 is priced (for cost logging) but must NOT be offered.
    """
    structured_output_capable = {
        "claude-haiku-4-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-fable-5",
    }
    offered = {c.id for c in SUPPORTED_MODELS}
    assert offered <= structured_output_capable
    assert "claude-sonnet-4-6" not in offered


def test_default_config_model_is_offered() -> None:
    assert AIConfig().model in {c.id for c in SUPPORTED_MODELS}


def test_meter_accumulates_scan_and_total() -> None:
    meter = EvalMeter()
    meter.record("claude-haiku-4-5", 1_000_000, 0)
    meter.record("claude-haiku-4-5", 1_000_000, 0)
    snap = meter.snapshot()
    assert snap["scan"]["evals"] == 2
    assert snap["scan"]["cost"] == 2.0
    assert snap["total"]["cost"] == 2.0


def test_start_scan_resets_only_the_scan_tally() -> None:
    meter = EvalMeter()
    meter.record("claude-haiku-4-5", 1_000_000, 0)
    meter.start_scan()
    meter.record("claude-haiku-4-5", 1_000_000, 0)
    snap = meter.snapshot()
    assert snap["scan"]["cost"] == 1.0
    assert snap["total"]["cost"] == 2.0
    assert snap["total"]["evals"] == 2


def test_unknown_model_marks_cost_unknown_but_still_counts_evals() -> None:
    meter = EvalMeter()
    assert meter.record("mystery-model", 500, 50) is None
    snap = meter.snapshot()
    assert snap["scan"]["evals"] == 1
    assert snap["scan"]["cost_known"] is False


def test_meter_is_thread_safe() -> None:
    import threading

    meter = EvalMeter()

    def hammer() -> None:
        for _ in range(200):
            meter.record("claude-haiku-4-5", 100, 10)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert meter.snapshot()["total"]["evals"] == 1600
