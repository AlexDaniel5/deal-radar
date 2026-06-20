"""Tests for the ntfy notifier (httpx MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from deal_radar.config.schema import NtfyNotifierConfig
from deal_radar.errors import NotifyError
from deal_radar.models import Evaluation, Listing, NotificationEvent
from deal_radar.notifiers.ntfy import NtfyNotifier


def _event() -> NotificationEvent:
    listing = Listing(
        id="1",
        marketplace="facebook",
        title="Gaming PC",
        url="https://x/1",
        price=500.0,
        location="Toronto",
    )
    evaluation = Evaluation(match=True, rating=5, rationale="great deal", model="claude-haiku-4-5")
    return NotificationEvent(item_name="Gaming PC", listing=listing, evaluation=evaluation)


def test_ntfy_publishes_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfyNotifier(NtfyNotifierConfig(topic="deal-radar-test"), client=client).notify(_event())

    assert "ntfy.sh" in captured["url"]
    body = captured["json"]
    assert body["topic"] == "deal-radar-test"
    assert "Gaming PC" in body["title"]
    assert "great deal" in body["message"]
    assert body["click"] == "https://x/1"


def test_ntfy_includes_priority_when_set() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = NtfyNotifierConfig(topic="t", priority=5)
    NtfyNotifier(config, client=client).notify(_event())
    assert captured["json"]["priority"] == 5


def test_ntfy_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(NotifyError):
        NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify(_event())
