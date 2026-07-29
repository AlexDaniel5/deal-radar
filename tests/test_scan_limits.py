"""Tests for the scan: config block, cost estimates, and the free test scan."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deal_radar.config.loader import validate_config_text
from deal_radar.config.schema import AppConfig, ScanConfig
from deal_radar.web.app import create_app
from deal_radar.web.controller import ScannerController

VALID_CONFIG = """version: 1
ai: {model: claude-haiku-4-5, min_rating: 4}
marketplaces: {facebook: {enabled: true}}
notifiers: [{type: ntfy, topic: t}]
items:
  - {name: PC, marketplaces: [facebook], search_phrases: [gaming pc], description: d}
  - {name: Bike, marketplaces: [facebook], search_phrases: [bike], description: d}
"""


# --- schema -------------------------------------------------------------------


def test_scan_block_defaults_are_conservative() -> None:
    """25/item is a nickel a scan; the old hardcoded 100 was a first-run footgun."""
    scan = ScanConfig()
    assert scan.max_evaluations_per_item == 25
    assert scan.max_listings_per_search == 200


def test_config_without_a_scan_block_still_validates() -> None:
    """Back-compat: extra='forbid' makes additions the risky direction, not omissions."""
    cfg = validate_config_text(VALID_CONFIG)
    assert cfg.scan.max_evaluations_per_item == 25


def test_scan_block_is_read_from_the_file() -> None:
    cfg = validate_config_text(
        VALID_CONFIG + "scan: {max_evaluations_per_item: 3, max_listings_per_search: 40}\n"
    )
    assert cfg.scan.max_evaluations_per_item == 3
    assert cfg.scan.max_listings_per_search == 40


def test_zero_evaluations_is_allowed() -> None:
    """0 is the free scrape-only mode; it must not be rejected as out of range."""
    assert validate_config_text(VALID_CONFIG + "scan: {max_evaluations_per_item: 0}\n")


# --- CLI overrides ---------------------------------------------------------------


def test_cli_flags_override_the_config() -> None:
    import argparse

    from deal_radar.cli import _scan_limits

    cfg = validate_config_text(VALID_CONFIG + "scan: {max_evaluations_per_item: 3}\n")
    from_config = _scan_limits(cfg, argparse.Namespace(limit=None, max_evals=None))
    assert from_config == (200, 3)
    overridden = _scan_limits(cfg, argparse.Namespace(limit=10, max_evals=1))
    assert overridden == (10, 1)


# --- pricing endpoint ------------------------------------------------------------


def _client(tmp_path: Path, config: str = VALID_CONFIG) -> TestClient:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(lambda s: s.wait(), lambda s: None),
        login_fn=lambda *a, **k: None,
    )
    return TestClient(app)


def test_pricing_quotes_a_cost_before_the_click(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/api/pricing").json()
    assert body["known"] is True
    assert body["items"] == 2
    assert body["max_listings_checked"] == 50  # 2 items x 25
    assert 0.05 < body["max_cost"] < 0.30


def test_pricing_says_unknown_rather_than_guessing(tmp_path: Path) -> None:
    """A made-up number would be worse than admitting we don't know."""
    body = _client(tmp_path, VALID_CONFIG.replace("claude-haiku-4-5", "some-future-model")).get(
        "/api/pricing"
    ).json()
    assert body["known"] is False
    assert body["max_cost"] is None


def test_pricing_handles_a_broken_config(tmp_path: Path) -> None:
    body = _client(tmp_path, "version: 1\n").get("/api/pricing").json()
    assert body["known"] is False


def test_status_includes_measured_spend(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/api/status").json()
    assert "spend" in body
    assert body["spend"]["scan"]["evals"] == 0


# --- the free scan ------------------------------------------------------------------


def test_free_scan_makes_no_ai_calls_and_no_detail_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the promise the UI makes: "Test scan (free)" cannot spend money.

    max_evaluations=0 must short-circuit before both the evaluator and the
    detail-page fetch (which is itself slow, and a page load Facebook sees).
    """
    monkeypatch.setenv("DEAL_RADAR_DATA_DIR", str(tmp_path / "data"))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(VALID_CONFIG)

    calls: dict[str, int] = {"evaluate": 0, "fetch_details": 0, "search": 0}

    from deal_radar.models import Listing

    class FakeMarketplace:
        def __enter__(self) -> FakeMarketplace:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def search(self, item: Any, ctx: Any) -> list[Listing]:
            calls["search"] += 1
            return [
                Listing(
                    id=f"{item.name}-{i}",
                    marketplace="facebook",
                    title="A thing",
                    url="https://example.com/x",
                    price=100.0,
                )
                for i in range(3)
            ]

        def fetch_details(self, listing: Listing) -> Listing:
            calls["fetch_details"] += 1
            return listing

    class ExplodingEvaluator:
        def evaluate(self, *a: Any, **k: Any) -> Any:
            calls["evaluate"] += 1
            raise AssertionError("a free scan must never call the AI")

    import deal_radar.web.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ClaudeEvaluator", lambda ai: ExplodingEvaluator())
    monkeypatch.setattr(runner_mod, "build_marketplace", lambda *a, **k: FakeMarketplace())
    monkeypatch.setattr(runner_mod, "build_notifier", lambda n: _NullNotifier())

    job = runner_mod.build_free_scan_job(str(cfg_path))
    job(threading.Event())

    assert calls["search"] > 0, "the scan should still have looked"
    assert calls["evaluate"] == 0
    assert calls["fetch_details"] == 0


class _NullNotifier:
    def notify_digest(self, item_name: str, events: Any) -> None:
        raise AssertionError("a free scan finds no matches, so it must not notify")


def test_free_scan_endpoint_reports_its_own_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEAL_RADAR_DATA_DIR", str(tmp_path / "data"))
    started = threading.Event()
    release = threading.Event()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    ctl = ScannerController(lambda s: s.wait(), lambda s: None)
    app = create_app(config_path=str(cfg), controller=ctl, login_fn=lambda *a, **k: None)

    import deal_radar.web.app as app_mod

    def fake_free(path: str) -> Any:
        def job(stop: threading.Event) -> None:
            started.set()
            release.wait(2.0)

        return job

    monkeypatch.setattr(app_mod, "build_free_scan_job", fake_free)
    client = TestClient(app)
    try:
        resp = client.post("/api/scanner/start", params={"mode": "once", "free": 1}, json={})
        assert resp.json()["started"] is True
        assert started.wait(2.0)
        assert client.get("/api/status").json()["mode"] == "free"
    finally:
        release.set()


def test_app_config_exposes_scan_defaults() -> None:
    assert AppConfig(
        notifiers=[{"type": "ntfy", "topic": "t"}],
        items=[
            {
                "name": "x",
                "marketplaces": ["facebook"],
                "search_phrases": ["y"],
                "description": "d",
            }
        ],
        marketplaces={"facebook": {}},
    ).scan.max_evaluations_per_item == 25
