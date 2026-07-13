"""Tests for the Claude evaluator (fake client) and prompt builder."""

from __future__ import annotations

from typing import Any

import pytest

from deal_radar.ai.claude import ClaudeEvaluator
from deal_radar.ai.composer import ClaudeComposer
from deal_radar.ai.prompt import DraftMessage, Verdict, build_compose_prompt, build_user_prompt
from deal_radar.config.schema import AIConfig, ItemConfig
from deal_radar.errors import EvalError
from deal_radar.models import Listing


class _Messages:
    def __init__(self, parsed: Any = None, exc: Exception | None = None) -> None:
        self._parsed = parsed
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return type("Resp", (), {"parsed_output": self._parsed})()


class _Client:
    def __init__(self, parsed: Any = None, exc: Exception | None = None) -> None:
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


def _photo_listing() -> Listing:
    return Listing(
        id="1",
        marketplace="facebook",
        title="RTX 3070 PC",
        url="u",
        price=600.0,
        description="Mid tower build, all parts listed in photos",
        image_urls=["https://cdn/img1.jpg", "https://cdn/img2.jpg"],
    )


def test_evaluate_attaches_all_photos_on_keyword() -> None:
    client = _Client(parsed=Verdict(match=True, rating=4, rationale="good"))
    evaluator = ClaudeEvaluator(AIConfig(analyze_images=True), client=client)
    result = evaluator.evaluate(_item(), _photo_listing())
    assert result.images_analyzed is True
    content = client.messages.calls[0]["messages"][0]["content"]
    assert isinstance(content, list)
    images = [b for b in content if b["type"] == "image"]
    assert [b["source"]["url"] for b in images] == ["https://cdn/img1.jpg", "https://cdn/img2.jpg"]
    assert content[-1]["type"] == "text"
    assert "RTX 3070 PC" in content[-1]["text"]


def test_evaluate_text_only_without_photo_keyword() -> None:
    client = _Client(parsed=Verdict(match=True, rating=4, rationale="good"))
    evaluator = ClaudeEvaluator(AIConfig(analyze_images=True), client=client)
    listing = _photo_listing()
    listing.description = "Mid tower build, great condition"
    evaluator.evaluate(_item(), listing)
    assert isinstance(client.messages.calls[0]["messages"][0]["content"], str)


def test_evaluate_text_only_when_images_disabled() -> None:
    client = _Client(parsed=Verdict(match=True, rating=4, rationale="good"))
    evaluator = ClaudeEvaluator(AIConfig(analyze_images=False), client=client)
    evaluator.evaluate(_item(), _photo_listing())
    assert isinstance(client.messages.calls[0]["messages"][0]["content"], str)


def test_build_user_prompt_contains_fields() -> None:
    prompt = build_user_prompt(_item(), _listing())
    assert "RTX 3070 PC" in prompt
    assert "600" in prompt
    assert "RTX 3070 desktop" in prompt


def test_build_compose_prompt_with_offer() -> None:
    prompt = build_compose_prompt(_item(), _listing(), 450)
    assert "opening offer of 450 USD" in prompt
    assert "RTX 3070 PC" in prompt


def test_build_compose_prompt_availability_only() -> None:
    prompt = build_compose_prompt(_item(), _listing(), None)
    assert "still available" in prompt
    assert "offer" not in prompt.lower()


def test_composer_returns_message_text() -> None:
    client = _Client(parsed=DraftMessage(message="Hi! Is this still available?"))
    composer = ClaudeComposer(AIConfig(model="claude-haiku-4-5"), client=client)
    text = composer.compose(_item(), _listing(), 450)
    assert text == "Hi! Is this still available?"
    call = client.messages.calls[0]
    assert call["output_format"] is DraftMessage
    assert "450" in call["messages"][0]["content"]


def test_composer_none_output_raises() -> None:
    composer = ClaudeComposer(AIConfig(), client=_Client(parsed=None))
    with pytest.raises(EvalError):
        composer.compose(_item(), _listing(), None)


def test_composer_wraps_sdk_error() -> None:
    composer = ClaudeComposer(AIConfig(), client=_Client(exc=RuntimeError("boom")))
    with pytest.raises(EvalError, match="boom"):
        composer.compose(_item(), _listing(), None)
