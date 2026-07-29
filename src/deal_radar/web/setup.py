"""The actions behind the setup wizard: the ones that touch the network or a browser.

Kept apart from :mod:`deal_radar.web.preflight`, which must stay pure so it can
run on the event loop. Everything here either blocks or costs money, so it runs
on a worker thread or is explicitly triggered by the user.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..config.loader import load_config, load_dotenv_if_present
from ..config.schema import AppConfig, MarketplaceConfig
from ..errors import DealRadarError, NotifyError
from ..logging import get_logger
from ..marketplaces.facebook import capture_session, resolve_session_path
from ..models import Evaluation, Listing, NotificationEvent
from ..notifiers.registry import IMPLEMENTED_NOTIFIERS, build_notifier
from .dotenv_io import read_env_var, remove_env_var, upsert_env_var
from .worker import BackgroundJob

log = get_logger("web.setup")

# How long the browser stays open waiting for someone to finish signing in.
LOGIN_TIMEOUT_SECONDS = 600.0
# How long the session probe waits for Marketplace to show listings.
SESSION_PROBE_TIMEOUT_MS = 15_000


class SetupError(DealRadarError):
    """A setup action failed in a way worth showing the user verbatim."""


# --- remembered verification results -------------------------------------------


def _state_path() -> Path:
    return paths.data_dir() / "setup.json"


def read_state() -> dict[str, Any]:
    """Timestamps of things we've verified. Never the source of truth for readiness.

    Readiness is always re-derived from live checks, because a deleted session
    file or a revoked key must re-trigger the wizard. This only remembers *when*
    a slow check last passed, so the UI can say "checked 2 days ago".
    """
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(**updates: Any) -> dict[str, Any]:
    state = read_state()
    state.update(updates)
    path = paths.ensure_data_dir() / "setup.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


# --- API key --------------------------------------------------------------------

# Anthropic keys start with this; checked so an obvious paste error (a URL, a
# whole curl command) is caught before it is written to disk.
_KEY_PREFIX = "sk-ant-"


def env_file_path(config_path: str | Path) -> Path:
    """The ``.env`` we manage — next to the config file the user is editing."""
    return Path(config_path).resolve().parent / ".env"


def mask_key(secret: str) -> str:
    return f"…{secret[-4:]}" if len(secret) >= 8 else "…"


def save_api_key(config_path: str | Path, cfg: AppConfig | None, key: str) -> dict[str, Any]:
    """Write the key to ``.env`` and make it live in this process.

    Setting ``os.environ`` explicitly is not redundant: ``load_dotenv_if_present``
    uses ``override=False``, so if a *wrong* key is already in the environment,
    re-reading the file would not replace it and the user would be told their
    correct new key doesn't work.
    """
    key = key.strip()
    if not key:
        raise SetupError("No key was provided.")
    if any(c.isspace() for c in key):
        raise SetupError("That doesn't look like a key — it contains spaces.")
    if not key.startswith(_KEY_PREFIX):
        raise SetupError(
            f"Anthropic keys start with '{_KEY_PREFIX}'. Copy the whole key from "
            "the Anthropic console and paste it again."
        )
    name = cfg.ai.api_key_env if cfg else "ANTHROPIC_API_KEY"
    path = upsert_env_var(env_file_path(config_path), name, key)
    os.environ[name] = key
    load_dotenv_if_present(path)
    log.info("API key saved to %s as %s (%s)", path, name, mask_key(key))
    return {"ok": True, "hint": mask_key(key), "path": str(path), "env_var": name}


def clear_api_key(config_path: str | Path, cfg: AppConfig | None) -> dict[str, Any]:
    name = cfg.ai.api_key_env if cfg else "ANTHROPIC_API_KEY"
    path = env_file_path(config_path)
    removed = remove_env_var(path, name)
    os.environ.pop(name, None)
    log.info("API key removed from %s", path)
    return {"ok": True, "removed": removed, "env_var": name}


def test_api_key(cfg: AppConfig, *, deep: bool = False) -> dict[str, Any]:
    """Check the key works — and, with ``deep``, that the account can be billed.

    The shallow check retrieves the configured model: it costs nothing, and it
    validates both the key *and* that the model name in the config is real.
    It cannot prove there is credit on the account, which is why ``deep`` exists.
    """
    import anthropic

    name = cfg.ai.api_key_env
    key = os.environ.get(name, "").strip()
    if not key:
        raise SetupError("No key is saved yet.")
    client = anthropic.Anthropic(api_key=key)
    try:
        if deep:
            client.messages.create(
                model=cfg.ai.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        else:
            client.models.retrieve(cfg.ai.model)
    except anthropic.AuthenticationError as exc:
        raise SetupError("That key wasn't accepted. Check you copied all of it.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise SetupError("That key doesn't have permission to use this model.") from exc
    except anthropic.NotFoundError as exc:
        raise SetupError(
            f"The AI model in your settings ({cfg.ai.model}) doesn't exist. "
            "Pick one from the list in Settings."
        ) from exc
    except anthropic.APIStatusError as exc:
        detail = getattr(exc, "message", str(exc))
        if exc.status_code == 400 and "credit" in str(detail).lower():
            raise SetupError(
                "The key works, but the account has no credit. Add credit in the "
                "Anthropic console, then try again."
            ) from exc
        raise SetupError(f"Anthropic returned an error: {detail}") from exc
    except anthropic.APIConnectionError as exc:
        raise SetupError("Couldn't reach Anthropic. Check your internet connection.") from exc
    write_state(key_verified_ts=time.time(), key_verified_deep=deep)
    return {
        "ok": True,
        "deep": deep,
        "model": cfg.ai.model,
        "message": (
            "Your key works and the account can be billed."
            if deep
            else (
                "Your key works. (This doesn't check you have credit — "
                "use the full test for that.)"
            )
        ),
    }


# --- notifications ----------------------------------------------------------------


def send_test_notification(cfg: AppConfig) -> dict[str, Any]:
    """Push one obviously-fake match through every configured notifier."""
    if not cfg.notifiers:
        raise SetupError("You haven't set up anywhere to send alerts.")
    listing = Listing(
        id="test",
        marketplace="facebook",
        title="Test alert from deal-radar",
        url="https://example.com/deal-radar-test",
        price=1.0,
        description="If you can read this on your phone, alerts are working.",
    )
    evaluation = Evaluation(
        match=True, rating=5, rationale="This is a test alert.", model="test"
    )
    event = NotificationEvent(item_name="Test", listing=listing, evaluation=evaluation)
    sent, failures = 0, []
    for notifier_cfg in cfg.notifiers:
        if notifier_cfg.type not in IMPLEMENTED_NOTIFIERS:
            failures.append(f"'{notifier_cfg.type}' alerts aren't built yet.")
            continue
        try:
            build_notifier(notifier_cfg).notify_digest("Test", [event])
            sent += 1
        except (NotifyError, NotImplementedError) as exc:
            failures.append(str(exc))
    if not sent:
        raise SetupError(" ".join(failures) or "The alert could not be sent.")
    return {
        "ok": True,
        "sent": sent,
        "message": "Sent. Check your phone — it may take a few seconds.",
        "warnings": failures,
    }


# --- Facebook: session probe and login ---------------------------------------------


def _facebook_config(cfg: AppConfig) -> MarketplaceConfig:
    mk_cfg = cfg.marketplaces.get("facebook")
    if mk_cfg is None:
        raise SetupError("Facebook isn't set up as a place to look.")
    return mk_cfg


def check_facebook_session(cfg: AppConfig) -> dict[str, Any]:
    """Actually open Marketplace with the saved session and see if we're signed in.

    This is the check that closes the product's worst silent failure: an expired
    session makes a scan finish cleanly with zero results, because the scraper
    just logs a warning and skips the search.

    Runs a browser, so it must be called from a worker thread.
    """
    from playwright.sync_api import sync_playwright

    mk_cfg = _facebook_config(cfg)
    session_path = resolve_session_path(mk_cfg)
    if not session_path.is_file():
        return _remember_session(False, "You haven't signed in to Facebook yet.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=str(session_path))
            page = context.new_page()
            page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
            if "login" in page.url or page.locator("input[name='pass']").count():
                return _remember_session(
                    False, "Facebook is asking you to sign in again — the saved sign-in expired."
                )
            try:
                page.wait_for_selector(
                    'a[href*="/marketplace/item/"]', timeout=SESSION_PROBE_TIMEOUT_MS
                )
            except Exception:  # noqa: BLE001 - a timeout here is the answer, not an error
                return _remember_session(
                    False,
                    "Signed in, but Facebook didn't show any listings. The sign-in may have "
                    "expired, or Facebook may be asking for a security check — sign in again.",
                )
            return _remember_session(True, "Your Facebook sign-in works.")
        finally:
            browser.close()


def _remember_session(ok: bool, message: str) -> dict[str, Any]:
    write_state(fb_checked_ts=time.time(), fb_checked_ok=ok)
    log.info("facebook session check: %s (%s)", "ok" if ok else "not usable", message)
    return {"ok": ok, "message": message}


@dataclass
class LoginState:
    """What the browser-login flow is currently doing, for polling from the UI."""

    state: str = "idle"  # idle | opening | waiting | saving | done | error
    message: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "message": self.message, "error": self.error}


class FacebookLogin:
    """Drives the one-time headful Facebook sign-in from the browser.

    ``capture_session`` is synchronous and blocks inside ``wait_for_login`` while
    a real browser window is open, so it runs on a worker thread. Cancelling and
    timing out both work by *raising* out of ``wait_for_login``: that unwinds
    ``capture_session``'s ``with sync_playwright()`` block, which closes the
    browser cleanly.
    """

    def __init__(self, login_fn: Any = capture_session) -> None:
        self._login_fn = login_fn
        self._job = BackgroundJob("deal-radar-fb-login")
        self._done = threading.Event()
        self._cancelled = False
        self._state = LoginState()

    def start(self, cfg: AppConfig) -> bool:
        mk_cfg = _facebook_config(cfg)
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise SetupError(
                "This computer can't open a browser window right now, so signing in "
                "here isn't possible. Someone can run 'deal-radar login facebook' "
                "in a terminal instead."
            )
        self._done = threading.Event()
        self._cancelled = False
        self._state = LoginState("opening", "Opening a browser window…")
        started = self._job.start(lambda _stop: self._run(mk_cfg), on_finish=lambda: None)
        if not started:
            raise SetupError("A sign-in is already in progress.")
        return True

    def _run(self, mk_cfg: MarketplaceConfig) -> None:
        def wait_for_login() -> None:
            if not self._done.wait(timeout=LOGIN_TIMEOUT_SECONDS):
                raise SetupError(
                    "Timed out waiting for the sign-in. The browser window has been closed."
                )
            if self._cancelled:
                raise SetupError("Sign-in cancelled.")
            self._state = LoginState("saving", "Saving your sign-in…")

        def on_open() -> None:
            self._state = LoginState(
                "waiting",
                "Sign in to Facebook in the window that opened, then click "
                "“I'm signed in” below.",
            )

        try:
            path = self._login_fn(mk_cfg, wait_for_login=wait_for_login, on_open=on_open)
        except Exception as exc:  # noqa: BLE001 - shown to the user as-is
            self._state = LoginState("error", "", str(exc))
            log.warning("facebook login failed: %s", exc)
            return
        self._state = LoginState("done", "Signed in. Your sign-in has been saved.")
        write_state(fb_checked_ts=time.time(), fb_checked_ok=True)
        log.info("facebook session saved to %s", path)

    def finish(self) -> None:
        """The user says they've signed in; let capture_session save the state."""
        self._done.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._done.set()

    def is_busy(self) -> bool:
        return self._job.is_busy()

    def status(self) -> dict[str, Any]:
        return {**self._state.as_dict(), "busy": self.is_busy()}


# --- doctor (shared by the CLI) --------------------------------------------------


def load_config_or_none(config_path: str | Path) -> AppConfig | None:
    try:
        return load_config(config_path, resolve_env=False)
    except DealRadarError:
        return None


def read_env_from_file(config_path: str | Path, name: str) -> str | None:
    return read_env_var(env_file_path(config_path), name)
