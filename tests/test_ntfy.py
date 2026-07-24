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
    assert body["tags"] == ["moneybag"]


def test_ntfy_adds_camera_tag_when_images_analyzed() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    event = _event()
    event.evaluation.images_analyzed = True
    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify(event)
    assert captured["json"]["tags"] == ["moneybag", "camera"]


def test_ntfy_includes_priority_when_set() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = NtfyNotifierConfig(topic="t", priority=5)
    NtfyNotifier(config, client=client).notify(_event())
    assert captured["json"]["priority"] == 5


def _event_for(listing_id: str, *, rating: int, price: float) -> NotificationEvent:
    listing = Listing(
        id=listing_id,
        marketplace="facebook",
        title=f"PC {listing_id}",
        url=f"https://x/{listing_id}",
        price=price,
        location="Toronto",
    )
    evaluation = Evaluation(match=True, rating=rating, rationale="r", model="m")
    return NotificationEvent(item_name="Laptop", listing=listing, evaluation=evaluation)


def test_ntfy_digest_lists_all_matches_and_links_top_pick() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    events = [
        _event_for("c", rating=5, price=500.0),
        _event_for("b", rating=5, price=700.0),
        _event_for("a", rating=4, price=900.0),
    ]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify_digest("Laptop", events)

    body = captured["json"]
    assert body["title"] == "Top 3 Laptop deals"
    # Every listing appears, numbered, with its own tappable URL.
    for rank, event in enumerate(events, start=1):
        assert f"{rank}." in body["message"]
        assert event.listing.url in body["message"]
    assert body["click"] == "https://x/c"  # the #1 pick


def test_ntfy_digest_single_event_falls_back_to_plain_alert() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify_digest("Laptop", [_event()])
    # One match -> the normal "[5/5] item" title, not a "Top N" digest.
    assert captured["json"]["title"].startswith("[5/5]")


def test_ntfy_digest_empty_sends_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not publish for an empty digest")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify_digest("Laptop", [])


def test_ntfy_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(NotifyError):
        NtfyNotifier(NtfyNotifierConfig(topic="t"), client=client).notify(_event())
