"""Tests for the pure parsing helpers in the Facebook adapter."""

from __future__ import annotations

from deal_radar.config.schema import ItemConfig, MarketplaceConfig
from deal_radar.marketplaces.facebook import (
    _DETAIL_MAIN_SELECTOR,
    _DETAIL_TEXT_SELECTORS,
    _card_is_relevant,
    _detect_currency,
    _extract_detail_text,
    _extract_item_id,
    _parse_card_text,
    _parse_price,
    _pick_detail_text,
    _relevance_tokens,
    build_search_url,
)


def _card(joined: str) -> str:
    """Turn the ' | '-joined debug form of a card into real newline-split text."""
    return "\n".join(part.strip() for part in joined.split("|"))


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


def test_parse_card_text_drops_badge_and_picks_real_title() -> None:
    # Real card with a "Just listed" badge and a struck-through original price.
    price, title, location = _parse_card_text(
        _card("Just listed | CA$1,050 | CA$1,200 | Gaming PC | Lincoln, ON")
    )
    assert price == 1050.0  # asking price, not the struck-through original
    assert title == "Gaming PC"
    assert location == "Lincoln, ON"


def test_parse_card_text_single_price() -> None:
    price, title, location = _parse_card_text(
        _card(
            "Just listed | CA$1,050 | "
            "Gaming PC - Intel i9-12900K, RTX 3070, 32GB RAM | Richmond Hill, ON"
        )
    )
    assert price == 1050.0
    assert title == "Gaming PC - Intel i9-12900K, RTX 3070, 32GB RAM"
    assert location == "Richmond Hill, ON"


def test_parse_card_text_bike() -> None:
    price, title, location = _parse_card_text(
        _card("Just listed | CA$650 | Felt F80 Road Bike XL | Toronto, ON")
    )
    assert price == 650.0
    assert title == "Felt F80 Road Bike XL"
    assert location == "Toronto, ON"


def test_detect_currency() -> None:
    assert _detect_currency("Just listed\nCA$650\nBike\nToronto, ON") == "CAD"
    assert _detect_currency("$650\nBike\nToronto, ON") == "USD"


def test_pick_detail_text_prefers_longest() -> None:
    body = "the longest candidate body"
    assert _pick_detail_text(["short", body]) == body


def test_pick_detail_text_ignores_blank() -> None:
    assert _pick_detail_text(["", "   ", "real text"]) == "real text"


def test_pick_detail_text_empty() -> None:
    assert _pick_detail_text([]) == ""


class _FakeEl:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Minimal Playwright-page stand-in for _extract_detail_text."""

    def __init__(self, *, og: str = "", elements: dict[str, str] | None = None) -> None:
        self._og = og
        self._elements = elements or {}

    def get_attribute(self, selector: str, name: str) -> str | None:
        return self._og or None

    def query_selector(self, selector: str) -> _FakeEl | None:
        text = self._elements.get(selector)
        return _FakeEl(text) if text is not None else None


def test_extract_detail_prefers_substantial_targeted_match() -> None:
    body = "RAM: 64GB DDR5. " * 30  # well over the min-useful threshold
    page = _FakePage(elements={_DETAIL_TEXT_SELECTORS[0]: body})
    assert _extract_detail_text(page) == body.strip()


def test_extract_detail_falls_back_to_main_region() -> None:
    # Targeted selectors miss and og is empty -> use the noisy main region.
    main_text = "Condition Used\nRAM: 64GB DDR5\nGPU: 3080 Ti"
    page = _FakePage(elements={_DETAIL_MAIN_SELECTOR: main_text})
    assert "64GB DDR5" in _extract_detail_text(page)


def test_extract_detail_short_og_still_falls_back_to_main() -> None:
    # A short og blurb is a stub; the richer main region should win.
    page = _FakePage(og="Gaming PC", elements={_DETAIL_MAIN_SELECTOR: "x" * 1000})
    assert len(_extract_detail_text(page)) == 1000


def test_extract_detail_caps_main_region() -> None:
    page = _FakePage(elements={_DETAIL_MAIN_SELECTOR: "y" * 9000})
    assert len(_extract_detail_text(page)) == 2500


def test_extract_detail_empty_when_nothing_matches() -> None:
    assert _extract_detail_text(_FakePage()) == ""


def test_extract_detail_drops_seller_chrome() -> None:
    # A real captured body: description, then the seller/ad section we want gone.
    main_text = (
        "Gaming PC RTX 2070\nCA$500\nDetails\nCondition\nNew\nPrice is firm\n"
        "Seller information\nSeller details\nTrevor Jordan\n(146)\n"
        "Highly rated on Marketplace\nAd\nHomes By John Bruce Robinson"
    )
    result = _extract_detail_text(_FakePage(elements={_DETAIL_MAIN_SELECTOR: main_text}))
    assert "Price is firm" in result
    assert "Seller" not in result and "Trevor Jordan" not in result


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


def _pc_item() -> ItemConfig:
    return ItemConfig(
        name="pc",
        marketplaces=["facebook"],
        search_phrases=["gaming pc", "gaming computer", "rtx 3080"],
        include_keywords=["rtx", "ryzen", "intel i7", "i9"],
        description="d",
    )


def test_relevance_tokens_from_phrases_and_includes() -> None:
    tokens = _relevance_tokens(_pc_item())
    assert {"gaming", "pc", "computer", "rtx", "3080", "ryzen", "intel", "i7", "i9"} <= tokens


def test_card_relevance_keeps_real_pcs() -> None:
    tokens = _relevance_tokens(_pc_item())
    assert _card_is_relevant("RTX 3080 | Ryzen 5 5600 Custom Gaming PC", tokens)
    assert _card_is_relevant("Custom Gaming Computer, i7", tokens)


def test_card_relevance_drops_facebook_padding() -> None:
    # The unrelated 'suggested' listings that a deep scroll scooped up.
    tokens = _relevance_tokens(_pc_item())
    assert not _card_is_relevant("Brand New Napoleon Rouge 625 Propane Gas BBQ Grill", tokens)
    assert not _card_is_relevant("SimSpace Golf Enclosure & Tee Turf Hitting Mat", tokens)
    assert not _card_is_relevant("Bosch VeroCafe 500 Series Espresso Machine", tokens)
    assert not _card_is_relevant("Valerion Fresnel ALR Projector Screen - 120", tokens)


def test_card_relevance_passes_through_when_no_tokens() -> None:
    assert _card_is_relevant("anything at all", set())


def test_card_relevance_drops_piece_sets() -> None:
    # "N pc"/"N pcs" abbreviates "piece" (furniture), not PC — must be dropped.
    tokens = _relevance_tokens(_pc_item())
    assert not _card_is_relevant("7 pc dining table set delivery available", tokens)
    assert not _card_is_relevant("3 pc sofa set delivery available", tokens)
    assert not _card_is_relevant("10pcs cookware set", tokens)
    # But a real "gaming pc" (no digit before pc) is still kept.
    assert _card_is_relevant("Gaming PC in great condition", tokens)
    assert _card_is_relevant("RTX 3080 Gaming PC", tokens)
