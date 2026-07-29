"""Tests for the /api/setup/* endpoints behind the first-run wizard.

No browser and no network: the Facebook login is driven through the injected
``login_fn`` seam, and the notifier/Anthropic calls are monkeypatched.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deal_radar.web import setup as setup_mod
from deal_radar.web.app import create_app
from deal_radar.web.controller import ScannerController
from deal_radar.web.setup import SetupError

VALID_CONFIG = """version: 1
ai: {model: claude-haiku-4-5, min_rating: 4}
marketplaces: {facebook: {enabled: true, session_path: SESSION}}
notifiers: [{type: ntfy, topic: t}]
items: [{name: PC, marketplaces: [facebook], search_phrases: [gaming pc], description: d}]
"""


def _wait(pred: Any, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A sandboxed install: own data dir, own config, own browser root."""
    monkeypatch.setenv("DEAL_RADAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    (tmp_path / "browsers" / "chromium-1").mkdir(parents=True)
    session = tmp_path / "facebook.json"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG.replace("SESSION", str(session)))
    return {"tmp": tmp_path, "cfg": cfg, "session": session}


def _client(env: dict[str, Any], login_fn: Any = None) -> TestClient:
    app = create_app(
        config_path=str(env["cfg"]),
        controller=ScannerController(lambda s: s.wait(), lambda s: None),
        login_fn=login_fn or (lambda *a, **k: None),
    )
    return TestClient(app)


# --- status -----------------------------------------------------------------


def test_status_lists_every_check_with_plain_language(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(setup_mod, "load_config_or_none", setup_mod.load_config_or_none)
    body = _client(env).get("/api/setup/status").json()
    ids = [c["id"] for c in body["checks"]]
    assert ids[:5] == ["config", "browser", "api_key", "facebook", "alerts"]
    for check in body["checks"]:
        assert check["label"] and check["detail"], check["id"]


def test_status_never_caches_the_masked_key(env: dict[str, Any]) -> None:
    resp = _client(env).get("/api/setup/status")
    assert resp.headers["cache-control"] == "no-store"


def test_status_reports_setup_required_when_something_blocks(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(env["tmp"] / "empty"))
    body = _client(env).get("/api/setup/status").json()
    assert body["setup_required"] is True
    browser = next(c for c in body["checks"] if c["id"] == "browser")
    assert browser["copyable"] == "playwright install chromium"


# --- API key ----------------------------------------------------------------


def test_save_key_writes_dotenv_and_never_echoes_the_key(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(env)
    resp = client.post("/api/setup/api-key", json={"key": "sk-ant-supersecret-WXYZ"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["hint"] == "…WXYZ"
    assert "supersecret" not in resp.text, "the key must never come back over the wire"
    # Written next to the config, not the current directory.
    assert "sk-ant-supersecret-WXYZ" in (env["tmp"] / ".env").read_text()


def test_saved_key_takes_effect_immediately(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_dotenv uses override=False, so the process env must be set directly.

    Without that, replacing a wrong key would appear to do nothing.
    """
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-0000")
    client = _client(env)
    client.post("/api/setup/api-key", json={"key": "sk-ant-fresh-1111"})
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-fresh-1111"


def test_save_key_rejects_obvious_paste_errors(env: dict[str, Any]) -> None:
    client = _client(env)
    for bad in ("", "hello world", "https://console.anthropic.com/keys"):
        resp = client.post("/api/setup/api-key", json={"key": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]


def test_delete_key(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    client = _client(env)
    client.post("/api/setup/api-key", json={"key": "sk-ant-abcdefgh"})
    # DELETE goes through the same write guard, so it carries the JSON header
    # (which is what the browser's api() helper does).
    resp = client.request("DELETE", "/api/setup/api-key", json={})
    assert resp.json()["ok"] is True
    assert os.environ.get("ANTHROPIC_API_KEY") is None


def test_delete_key_without_the_json_header_is_refused(env: dict[str, Any]) -> None:
    assert _client(env).delete("/api/setup/api-key").status_code == 415


def test_key_test_maps_auth_failure_to_plain_language(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    def boom(cfg: Any, *, deep: bool = False) -> Any:
        raise SetupError("That key wasn't accepted. Check you copied all of it.")

    monkeypatch.setattr(setup_mod, "test_api_key", boom)
    resp = _client(env).post("/api/setup/api-key/test", json={})
    assert resp.status_code == 400
    assert "wasn't accepted" in resp.json()["error"]


# --- notifications -----------------------------------------------------------


def test_test_notify_success(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup_mod, "send_test_notification", lambda cfg: {"ok": True, "sent": 1, "message": "Sent."}
    )
    assert _client(env).post("/api/setup/test-notify", json={}).json()["sent"] == 1


def test_test_notify_reports_failure_plainly(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cfg: Any) -> Any:
        raise SetupError("Could not reach ntfy.sh.")

    monkeypatch.setattr(setup_mod, "send_test_notification", boom)
    resp = _client(env).post("/api/setup/test-notify", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "Could not reach ntfy.sh."


# --- Facebook login ------------------------------------------------------------


class FakeLogin:
    """Stands in for capture_session: records the handshake, writes no browser."""

    def __init__(self, session: Path) -> None:
        self.session = session
        self.opened = threading.Event()

    def __call__(self, mk_cfg: Any, *, wait_for_login: Any, on_open: Any = None, **kw: Any) -> Path:
        if on_open:
            on_open()
        self.opened.set()
        wait_for_login()  # blocks until finish() or cancel()
        self.session.write_text('{"cookies": []}')
        return self.session


def test_login_flow_reaches_done(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    fake = FakeLogin(env["session"])
    client = _client(env, login_fn=fake)

    assert client.post("/api/setup/facebook/login", json={}).status_code == 202
    assert fake.opened.wait(3.0)
    assert _wait(lambda: client.get("/api/setup/facebook/login").json()["state"] == "waiting")

    client.post("/api/setup/facebook/login/finish", json={})
    assert _wait(lambda: client.get("/api/setup/facebook/login").json()["state"] == "done")
    assert env["session"].is_file()


def test_login_can_be_cancelled(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    fake = FakeLogin(env["session"])
    client = _client(env, login_fn=fake)
    client.post("/api/setup/facebook/login", json={})
    assert fake.opened.wait(3.0)
    client.post("/api/setup/facebook/login/cancel", json={})
    assert _wait(lambda: client.get("/api/setup/facebook/login").json()["state"] == "error")
    assert not env["session"].is_file(), "a cancelled login must not save a session"


def test_login_refused_without_a_display(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    resp = _client(env).post("/api/setup/facebook/login", json={})
    assert resp.status_code == 409
    assert "browser window" in resp.json()["error"]


def test_login_refused_while_a_scan_is_running(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scans, sends and logins all drive Playwright — only one at a time."""
    monkeypatch.setenv("DISPLAY", ":0")
    ctl = ScannerController(lambda s: s.wait(), lambda s: None)
    app = create_app(
        config_path=str(env["cfg"]), controller=ctl, login_fn=FakeLogin(env["session"])
    )
    client = TestClient(app)
    client.post("/api/scanner/start", params={"mode": "loop"}, json={})
    assert _wait(ctl.is_running)
    try:
        resp = client.post("/api/setup/facebook/login", json={})
        assert resp.status_code == 409
        assert "Stop the scan first" in resp.json()["error"]
    finally:
        ctl.stop()


def test_session_check_runs_off_the_event_loop(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        setup_mod, "check_facebook_session", lambda cfg: {"ok": True, "message": "It works."}
    )
    client = _client(env)
    assert client.post("/api/setup/facebook/check", json={}).status_code == 202
    assert _wait(lambda: client.get("/api/setup/facebook/check").json()["busy"] is False)
    assert client.get("/api/setup/facebook/check").json()["result"]["ok"] is True


def test_session_check_failure_is_reported(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        setup_mod,
        "check_facebook_session",
        lambda cfg: {"ok": False, "message": "The saved sign-in expired."},
    )
    client = _client(env)
    client.post("/api/setup/facebook/check", json={})
    assert _wait(lambda: client.get("/api/setup/facebook/check").json()["busy"] is False)
    result = client.get("/api/setup/facebook/check").json()["result"]
    assert result["ok"] is False
    assert "expired" in result["message"]
