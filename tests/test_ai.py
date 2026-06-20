"""Tests for the Claude evaluator (fake client) and prompt builder."""

from __future__ import annotations

from typing import Any

import pytest

from deal_radar.ai.claude import ClaudeEvaluator
from deal_radar.ai.prompt import Verdict, build_user_prompt
from deal_radar.config.schema import AIConfig, ItemConfig
from deal_radar.errors import EvalError
from deal_radar.models import Listing


class _Messages:
    def __init__(self, parsed: Verdict | None = None, exc: Exception | None = None) -> None:
        self._parsed = parsed
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return type("Resp", (), {"parsed_output": self._parsed})()


class _Client:
    def __init__(self, parsed: Verdict | None = None, exc: Exception | None = None) -> None:
        self.messages = _Messages(parsed, exc)


def _item() -> ItemConfig:
    return ItemConfig(
        name="Gaming PC",
        marketplaces=["facebook"],
        search_phrases=["gaming pc"],
        description="want an RTX 3070 desktop",
    )


def _listing() -> Listing:
    return Listing(id="1", marketplace="facebook", title="RTX 3070 PC", url="u", price=600.0)


def test_evaluate_maps_verdict() -> None:
    client = _Client(parsed=Verdict(match=True, rating=4, rationale="good"))
    evaluator = ClaudeEvaluator(AIConfig(model="claude-haiku-4-5"), client=client)
    result = evaluator.evaluate(_item(), _listing())
    assert result.match is True
    assert result.rating == 4
    assert result.rationale == "good"
    assert result.model == "claude-haiku-4-5"
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["output_format"] is Verdict


def test_evaluate_none_output_raises() -> None:
    evaluator = ClaudeEvaluator(AIConfig(), client=_Client(parsed=None))
    with pytest.raises(EvalError):
        evaluator.evaluate(_item(), _listing())


def test_evaluate_wraps_sdk_error() -> None:
    evaluator = ClaudeEvaluator(AIConfig(), client=_Client(exc=RuntimeError("boom")))
    with pytest.raises(EvalError, match="boom"):
        evaluator.evaluate(_item(), _listing())


def test_build_user_prompt_contains_fields() -> None:
    prompt = build_user_prompt(_item(), _listing())
    assert "RTX 3070 PC" in prompt
    assert "600" in prompt
    assert "RTX 3070 desktop" in prompt
