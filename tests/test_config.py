"""Tests for config loading, env resolution, and validation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from deal_radar.config import AppConfig, load_config
from deal_radar.errors import ConfigError

_VALID: dict[str, Any] = {
    "version": 1,
    "ai": {"model": "claude-haiku-4-5", "min_rating": 4},
    "marketplaces": {"facebook": {"enabled": True, "default_location": "Toronto, ON"}},
    "schedule": {"poll_interval_seconds": 1800},
    "notifiers": [{"type": "ntfy", "topic": "deal-radar-test"}],
    "items": [
        {
            "name": "Gaming PC",
            "marketplaces": ["facebook"],
            "search_phrases": ["gaming pc"],
            "price_min": 400,
            "price_max": 1100,
            "description": "A modern gaming desktop with an RTX 3070 or better.",
        }
    ],
}


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_valid_config_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID))
    assert isinstance(cfg, AppConfig)
    assert cfg.ai.model == "claude-haiku-4-5"
    assert cfg.items[0].effective_min_rating(cfg.ai) == 4
    assert cfg.notifiers[0].type == "ntfy"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_env_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DR_TEST_TOPIC", "resolved-topic")
    data = copy.deepcopy(_VALID)
    data["notifiers"] = [{"type": "ntfy", "topic": "${DR_TEST_TOPIC}"}]
    cfg = load_config(_write(tmp_path, data))
    assert cfg.notifiers[0].type == "ntfy"
    assert cfg.notifiers[0].topic == "resolved-topic"


def test_missing_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DR_TEST_ABSENT", raising=False)
    data = copy.deepcopy(_VALID)
    data["notifiers"] = [{"type": "ntfy", "topic": "${DR_TEST_ABSENT}"}]
    with pytest.raises(ConfigError, match="DR_TEST_ABSENT"):
        load_config(_write(tmp_path, data))


def test_unknown_marketplace_ref_raises(tmp_path: Path) -> None:
    data = copy.deepcopy(_VALID)
    data["items"][0]["marketplaces"] = ["craigslist"]
    with pytest.raises(ConfigError, match="unknown marketplace"):
        load_config(_write(tmp_path, data))


def test_price_min_gt_max_raises(tmp_path: Path) -> None:
    data = copy.deepcopy(_VALID)
    data["items"][0]["price_min"] = 2000
    data["items"][0]["price_max"] = 100
    with pytest.raises(ConfigError, match="price_min"):
        load_config(_write(tmp_path, data))


def test_min_rating_out_of_range_raises(tmp_path: Path) -> None:
    data = copy.deepcopy(_VALID)
    data["ai"]["min_rating"] = 9
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_unknown_field_rejected(tmp_path: Path) -> None:
    data = copy.deepcopy(_VALID)
    data["items"][0]["bogus_field"] = True
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_example_file_loads() -> None:
    # The committed template must always be valid (no env needed: ntfy topic is literal).
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    cfg = load_config(example)
    assert len(cfg.items) == 2
    assert "facebook" in cfg.marketplaces
