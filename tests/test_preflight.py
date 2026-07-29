"""Tests for the readiness checks behind the setup wizard.

Everything here must stay pure — no browser, no network. The fixtures below
build fake browser roots and session files on disk so the checks can be driven
into every state without touching the real machine.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from deal_radar.web import preflight
from deal_radar.web.preflight import (
    all_checks,
    check_alerts,
    check_api_key,
    check_browser,
    check_config,
    check_facebook,
    check_marketplaces,
    summarize,
)

VALID_CONFIG = """version: 1
ai: {model: claude-haiku-4-5, min_rating: 4}
marketplaces: {facebook: {enabled: true}}
notifiers: [{type: ntfy, topic: my-topic}]
items: [{name: PC, marketplaces: [facebook], search_phrases: [gaming pc], description: d}]
"""


def _by_id(checks: list[Any], check_id: str) -> Any:
    return next(c for c in checks if c.id == check_id)


# --- config ------------------------------------------------------------------


def test_config_missing_file_blocks(tmp_path: Path) -> None:
    check, cfg = check_config(tmp_path / "nope.yaml")
    assert check.state == "fail" and check.blocking
    assert cfg is None


def test_config_valid_counts_enabled_items(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG)
    check, cfg = check_config(path)
    assert check.state == "ok"
    assert "1 thing" in check.detail
    assert cfg is not None


def test_config_invalid_carries_field_locations(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nitems: []\nnotifiers: []\n")
    check, cfg = check_config(path)
    assert check.state == "fail"
    assert cfg is None
    # The pydantic detail must survive for the form to highlight fields.
    assert check.extra["kind"] == "schema"
    assert check.extra["errors"], "structured errors must not be swallowed"


def test_config_all_items_disabled_is_a_blocking_failure(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG.replace("{name: PC,", "{name: PC, enabled: false,"))
    check, _ = check_config(path)
    assert check.state == "fail"
    assert "paused" in check.detail


def test_config_read_never_resolves_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret in the environment must never be pulled into a browser-visible read.

    If this regressed, a user with `topic: ${NTFY_TOPIC}` would open the form,
    see the real secret, save, and write the plaintext into config.yaml.
    """
    monkeypatch.setenv("NTFY_TOPIC", "super-secret-value")
    path = tmp_path / "config.yaml"
    # Quoted: bare ${...} inside a YAML flow mapping is a syntax error.
    path.write_text(VALID_CONFIG.replace("topic: my-topic", 'topic: "${NTFY_TOPIC}"'))
    check, cfg = check_config(path)
    assert check.state == "ok"
    assert cfg is not None
    assert cfg.notifiers[0].topic == "${NTFY_TOPIC}"  # unresolved
    assert "super-secret-value" not in json.dumps(check.as_dict())


# --- browser -----------------------------------------------------------------


def test_browser_missing_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))
    check = check_browser()
    assert check.state == "fail" and check.blocking
    assert check.copyable == "playwright install chromium"


def test_browser_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "chromium-1234").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert check_browser().state == "ok"


def test_browser_headless_shell_only_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanning works, but the headful login the wizard offers does not."""
    (tmp_path / "chromium_headless_shell-1234").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    check = check_browser()
    assert check.state == "warn"
    assert check.blocking is False


# --- API key -----------------------------------------------------------------


def test_api_key_missing_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "load_dotenv_if_present", lambda *a, **k: None)
    check = check_api_key(None)
    assert check.state == "fail" and check.blocking
    assert check.action == "save_api_key"


def test_api_key_present_is_masked_and_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-abcdWXYZ")
    monkeypatch.setattr(preflight, "load_dotenv_if_present", lambda *a, **k: None)
    check = check_api_key(None)
    assert check.state == "warn"  # present but never tested
    assert "WXYZ" in check.detail
    assert "sk-ant-secret" not in json.dumps(check.as_dict()), "the key must never leak"


def test_api_key_verified_recently_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-abcdWXYZ")
    monkeypatch.setattr(preflight, "load_dotenv_if_present", lambda *a, **k: None)
    check = check_api_key(None, verified_ts=time.time())
    assert check.state == "ok"
    assert "just now" in check.detail


# --- Facebook session --------------------------------------------------------


def _session(
    tmp_path: Path,
    *,
    expires: float | int = -1,
    cookies: tuple[str, ...] = ("c_user", "xs"),
) -> Path:
    path = tmp_path / "facebook.json"
    path.write_text(
        json.dumps(
            {"cookies": [{"name": n, "value": "v", "expires": expires} for n in cookies]}
        )
    )
    return path


def _cfg_with_session(tmp_path: Path, session: Path) -> Any:
    from deal_radar.config.loader import validate_config_text

    return validate_config_text(
        VALID_CONFIG.replace(
            "{facebook: {enabled: true}}", f"{{facebook: {{session_path: {session}}}}}"
        )
    )


def test_facebook_no_session_blocks(tmp_path: Path) -> None:
    cfg = _cfg_with_session(tmp_path, tmp_path / "missing.json")
    check = check_facebook(cfg)
    assert check.state == "fail" and check.blocking
    assert check.action == "facebook_login"


def test_facebook_saved_but_unverified_warns(tmp_path: Path) -> None:
    """The silent-failure case: present, plausible, never actually checked."""
    cfg = _cfg_with_session(tmp_path, _session(tmp_path))
    check = check_facebook(cfg)
    assert check.state == "warn"
    assert check.blocking is False
    assert check.action == "facebook_check"


def test_facebook_expired_cookie_blocks(tmp_path: Path) -> None:
    cfg = _cfg_with_session(tmp_path, _session(tmp_path, expires=time.time() - 86_400))
    check = check_facebook(cfg)
    assert check.state == "fail"
    assert "expired" in check.detail


def test_facebook_damaged_session_blocks(tmp_path: Path) -> None:
    cfg = _cfg_with_session(tmp_path, _session(tmp_path, cookies=("datr",)))
    check = check_facebook(cfg)
    assert check.state == "fail"
    assert "damaged" in check.detail


def test_facebook_unparseable_session_blocks(tmp_path: Path) -> None:
    path = tmp_path / "facebook.json"
    path.write_text("not json at all")
    check = check_facebook(_cfg_with_session(tmp_path, path))
    assert check.state == "fail"


def test_facebook_verified_recently_is_ok(tmp_path: Path) -> None:
    cfg = _cfg_with_session(tmp_path, _session(tmp_path))
    check = check_facebook(cfg, checked={"ok": True, "ts": time.time()})
    assert check.state == "ok"


# --- alerts and marketplaces --------------------------------------------------


def test_alerts_ok_names_the_topic() -> None:
    from deal_radar.config.loader import validate_config_text

    check = check_alerts(validate_config_text(VALID_CONFIG))
    assert check.state == "ok"
    assert "my-topic" in check.detail


def test_unimplemented_notifier_blocks() -> None:
    """telegram validates in the schema but raises NotImplementedError at runtime."""
    from deal_radar.config.loader import validate_config_text

    cfg = validate_config_text(
        VALID_CONFIG.replace(
            "[{type: ntfy, topic: my-topic}]",
            "[{type: telegram, bot_token: t, chat_id: c}]",
        )
    )
    check = check_alerts(cfg)
    assert check.state == "fail" and check.blocking
    assert "aren't built yet" in check.detail


def test_unimplemented_marketplace_blocks() -> None:
    from deal_radar.config.loader import validate_config_text

    cfg = validate_config_text(
        VALID_CONFIG.replace(
            "{facebook: {enabled: true}}", "{facebook: {enabled: true}, kijiji: {enabled: true}}"
        )
    )
    check = check_marketplaces(cfg)
    assert check is not None
    assert check.state == "fail" and check.blocking
    assert "kijiji" in check.detail


def test_no_marketplace_warning_when_all_supported() -> None:
    from deal_radar.config.loader import validate_config_text

    assert check_marketplaces(validate_config_text(VALID_CONFIG)) is None


# --- rollup ------------------------------------------------------------------


def test_summarize_flags_setup_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "load_dotenv_if_present", lambda *a, **k: None)
    summary = summarize(all_checks(tmp_path / "nope.yaml"))
    assert summary["setup_required"] is True
    assert summary["ok"] is False
    assert summary["blocking_failures"] >= 3  # config, browser, key


def test_summarize_ok_when_only_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "chromium-1").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefghij")
    monkeypatch.setattr(preflight, "load_dotenv_if_present", lambda *a, **k: None)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        VALID_CONFIG.replace(
            "{facebook: {enabled: true}}",
            f"{{facebook: {{session_path: {_session(tmp_path)}}}}}",
        )
    )
    summary = summarize(all_checks(cfg_path))
    assert summary["ok"] is True
    assert summary["setup_required"] is False
    assert summary["warnings"] >= 1  # unverified key and/or session


def test_all_checks_never_launches_a_browser(tmp_path: Path) -> None:
    """A guard on the module's core promise: safe to call from the event loop."""
    import deal_radar.marketplaces.facebook as fb

    def explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("preflight must not launch Playwright")

    original = getattr(fb, "sync_playwright", None)
    fb.sync_playwright = explode  # type: ignore[attr-defined]
    try:
        all_checks(tmp_path / "config.yaml")
    finally:
        if original is None:
            del fb.sync_playwright  # type: ignore[attr-defined]
        else:  # pragma: no cover - defensive
            fb.sync_playwright = original  # type: ignore[attr-defined]
