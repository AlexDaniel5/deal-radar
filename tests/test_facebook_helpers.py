"""Tests for the pure parsing helpers in the Facebook adapter."""

from __future__ import annotations

from deal_radar.config.schema import ItemConfig, MarketplaceConfig
from deal_radar.marketplaces.facebook import (
    _extract_item_id,
    _parse_card_text,
    _parse_price,
    build_search_url,
)


def test_extract_item_id() -> None:
    assert _extract_item_id("/marketplace/item/123456/?ref=search") == "123456"
    assert _extract_item_id("https://www.facebook.com/marketplace/item/987/") == "987"
    assert _extract_item_id("/marketplace/category/electronics") is None


def test_parse_price() -> None:
    assert _parse_price("$1,200") == 1200.0
    assert _parse_price("CA$950.50") == 950.50
    assert _parse_price("Free") == 0.0
    assert _parse_price("Toronto, ON") is None
    assert _parse_price("") is None


def test_parse_card_text() -> None:
    price, title, location = _parse_card_text("$1,200\nGaming PC RTX 3070\nToronto, ON")
    assert price == 1200.0
    assert "Gaming PC" in title
    assert location == "Toronto, ON"


def test_parse_card_text_empty() -> None:
    price, title, location = _parse_card_text("")
    assert price is None
    assert title == ""
    assert location is None


def test_build_search_url() -> None:
    item = ItemConfig(
        name="x",
        marketplaces=["facebook"],
        search_phrases=["gaming pc"],
        price_min=400,
        price_max=1100,
        radius_km=50,
        description="d",
    )
    url = build_search_url("gaming pc", item, MarketplaceConfig())
    assert "query=gaming+pc" in url
    assert "minPrice=400" in url
    assert "maxPrice=1100" in url
    assert "radius=50" in url
