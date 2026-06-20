"""Tests for the core domain types."""

from __future__ import annotations

from deal_radar.models import Evaluation, Listing, NotificationEvent


def test_listing_defaults() -> None:
    listing = Listing(id="abc", marketplace="facebook", title="Gaming PC", url="https://x/y")
    assert listing.price is None
    assert listing.currency == "USD"
    assert listing.image_urls == []


def test_listing_image_urls_are_independent() -> None:
    a = Listing(id="1", marketplace="facebook", title="t", url="u")
    b = Listing(id="2", marketplace="facebook", title="t", url="u")
    a.image_urls.append("https://img/1.jpg")
    assert b.image_urls == []  # no shared mutable default


def test_notification_event_round_trip() -> None:
    listing = Listing(id="abc", marketplace="facebook", title="Gaming PC", url="https://x/y")
    evaluation = Evaluation(match=True, rating=5, rationale="great deal", model="claude-haiku-4-5")
    event = NotificationEvent(item_name="Gaming PC", listing=listing, evaluation=evaluation)
    assert event.evaluation.rating == 5
    assert event.listing.id == "abc"
