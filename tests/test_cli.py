"""Tests for CLI argument parsing and item selection."""

from __future__ import annotations

import signal

import pytest

from deal_radar.cli import _graceful_sigint, _select_items, build_parser
from deal_radar.config.schema import AppConfig, ItemConfig, MarketplaceConfig
from deal_radar.errors import ConfigError


def test_log_level_before_subcommand() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "validate-config"])
    assert args.log_level == "DEBUG"
    assert args.command == "validate-config"


def test_log_level_after_subcommand() -> None:
    args = build_parser().parse_args(["run-once", "--dry-run", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"
    assert args.command == "run-once"
    assert args.dry_run is True


def test_log_level_defaults_to_info() -> None:
    # When unset, the namespace omits the attribute; main() supplies the INFO default.
    args = build_parser().parse_args(["validate-config"])
    assert getattr(args, "log_level", "INFO") == "INFO"


def test_run_once_defaults() -> None:
    args = build_parser().parse_args(["run-once"])
    assert args.limit == 200
    assert args.max_evals == 100
    assert args.headful is False
    assert args.config == "config.yaml"


def test_run_defaults() -> None:
    args = build_parser().parse_args(["run"])
    assert args.command == "run"
    assert args.limit == 200
    assert args.max_evals == 100
    assert args.dry_run is False
    assert args.headful is False
    assert args.max_cycles is None


def test_run_accepts_max_cycles_and_item() -> None:
    args = build_parser().parse_args(["run", "--max-cycles", "3", "--item", "Gaming PC"])
    assert args.max_cycles == 3
    assert args.item == ["Gaming PC"]


def test_item_is_repeatable() -> None:
    args = build_parser().parse_args(["run-once", "--item", "pc", "--item", "bike"])
    assert args.item == ["pc", "bike"]


def _two_item_cfg() -> AppConfig:
    def item(name: str) -> ItemConfig:
        return ItemConfig(
            name=name, marketplaces=["facebook"], search_phrases=["x"], description="d"
        )

    return AppConfig(
        marketplaces={"facebook": MarketplaceConfig()},
        notifiers=[{"type": "ntfy", "topic": "t"}],
        items=[item("Gaming PC (RTX 30-series)"), item("Road bike (54-56cm)")],
    )


def test_select_items_none_returns_all() -> None:
    cfg = _two_item_cfg()
    assert [i.name for i in _select_items(cfg, None)] == [
        "Gaming PC (RTX 30-series)",
        "Road bike (54-56cm)",
    ]


def test_select_items_substring_case_insensitive() -> None:
    cfg = _two_item_cfg()
    assert [i.name for i in _select_items(cfg, ["PC"])] == ["Gaming PC (RTX 30-series)"]
    assert [i.name for i in _select_items(cfg, ["bike"])] == ["Road bike (54-56cm)"]


def test_select_items_multiple_patterns_no_duplicates() -> None:
    cfg = _two_item_cfg()
    # 'gaming' and 'pc' both hit the same item; it should appear once.
    names = [i.name for i in _select_items(cfg, ["pc", "bike", "gaming"])]
    assert names == ["Gaming PC (RTX 30-series)", "Road bike (54-56cm)"]


def test_select_items_unknown_pattern_raises() -> None:
    cfg = _two_item_cfg()
    with pytest.raises(ConfigError, match="boat"):
        _select_items(cfg, ["boat"])


def test_graceful_sigint_escalates_then_restores() -> None:
    original = signal.getsignal(signal.SIGINT)
    with _graceful_sigint():
        handler = signal.getsignal(signal.SIGINT)
        assert handler is not original
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)  # first Ctrl-C: graceful stop
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)  # second Ctrl-C: force quit
        # force-quit handed control back to Python's default handler
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert signal.getsignal(signal.SIGINT) is original  # restored on context exit
