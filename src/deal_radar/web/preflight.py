"""Are we actually set up to scan? — plain-language readiness checks.

Three things must be true before a first scan can work, and all three happen
outside the browser today: Chromium must be downloaded, an Anthropic API key
must be in the environment, and a Facebook session must be saved. Previously
the UI mentioned none of them and a missing one surfaced as a one-line error
three seconds after clicking Scan.

Everything here is **pure**: filesystem, environment, and config only. Nothing
in this module may launch a browser or make a network call — it backs a status
endpoint that runs on the event loop, and Playwright's sync API would deadlock
there. The authoritative browser/network probes live in ``web/setup.py`` and
run on worker threads.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config.loader import load_dotenv_if_present, validate_config_text
from ..config.schema import AppConfig
from ..errors import ConfigError
from ..marketplaces.facebook import read_session_state, resolve_session_path
from ..marketplaces.registry import IMPLEMENTED_MARKETPLACES
from ..notifiers.registry import IMPLEMENTED_NOTIFIERS

# State meanings:
#   ok      — verified good (as far as this cheap check can tell)
#   warn    — usable, but something needs attention
#   fail    — scanning cannot work until this is fixed
#   unknown — we can't tell without a slower check the user must trigger
State = str


@dataclass(frozen=True)
class Check:
    """One readiness item, written for someone who won't open a terminal."""

    id: str
    label: str
    state: State
    detail: str
    #: Blocking failures stop a scan from working at all, and gate the wizard.
    blocking: bool = False
    #: What the user (or their helper) should do about it.
    fix: str | None = None
    #: A command to show in a copy-to-clipboard box, when the fix is a command.
    copyable: str | None = None
    #: Name of a `/api/setup/*` action that can resolve or verify this in-browser.
    action: str | None = None
    #: Extra machine-readable context for the UI (never secrets).
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _playwright_browsers_root() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ms-playwright"


def check_browser() -> Check:
    """Is a Chromium build downloaded?

    Deliberately a file probe, not a launch: this runs on the event loop. It
    can't detect a *stale* build that Playwright would reject for a version
    mismatch — the wizard's explicit "Check browser" action does a real
    headless launch for that.
    """
    root = _playwright_browsers_root()
    full = sorted(root.glob("chromium-*")) if root.is_dir() else []
    headless_only = sorted(root.glob("chromium_headless_shell-*")) if root.is_dir() else []
    if full:
        return Check(
            id="browser",
            label="The web browser it uses",
            state="ok",
            detail="Installed.",
            blocking=True,
            extra={"root": str(root)},
        )
    if headless_only:
        return Check(
            id="browser",
            label="The web browser it uses",
            state="warn",
            detail=(
                "A partial browser is installed. Scanning will work, but signing in to "
                "Facebook from here needs the full one."
            ),
            blocking=False,
            fix="Ask whoever set this up for you to run this once:",
            copyable="playwright install chromium",
            extra={"root": str(root)},
        )
    return Check(
        id="browser",
        label="The web browser it uses",
        state="fail",
        detail="Not installed yet. deal-radar needs its own browser to read listings.",
        blocking=True,
        fix="Ask whoever set this up for you to run this once:",
        copyable="playwright install chromium",
        extra={"root": str(root)},
    )


def check_config(config_path: str | Path) -> tuple[Check, AppConfig | None]:
    """Does a settings file exist and make sense?"""
    path = Path(config_path)
    if not path.is_file():
        return (
            Check(
                id="config",
                label="Your settings",
                state="fail",
                detail="You haven't set anything up yet.",
                blocking=True,
                fix="Tell deal-radar what you're hunting for and where to send alerts.",
                action="open_settings",
                extra={"path": str(path), "exists": False},
            ),
            None,
        )
    try:
        # resolve_env=False: never pull secrets out of the environment and into
        # anything the browser can see.
        cfg = validate_config_text(path.read_text(encoding="utf-8"), resolve_env=False)
    except ConfigError as exc:
        return (
            Check(
                id="config",
                label="Your settings",
                state="fail",
                detail=str(exc),
                blocking=True,
                fix="Fix the highlighted settings, or edit the file directly under Advanced.",
                action="open_settings",
                extra={"path": str(path), "exists": True, "kind": exc.kind, "errors": exc.errors},
            ),
            None,
        )
    enabled = [i for i in cfg.items if i.enabled]
    if not enabled:
        return (
            Check(
                id="config",
                label="Your settings",
                state="fail",
                detail="Everything you're hunting for is paused, so a scan would do nothing.",
                blocking=True,
                fix="Switch at least one back on.",
                action="open_settings",
                extra={"path": str(path), "exists": True},
            ),
            cfg,
        )
    noun = "thing" if len(enabled) == 1 else "things"
    return (
        Check(
            id="config",
            label="Your settings",
            state="ok",
            detail=f"{len(enabled)} {noun} you're hunting for.",
            blocking=True,
            extra={"path": str(path), "exists": True, "items": [i.name for i in enabled]},
        ),
        cfg,
    )


def _mask(secret: str) -> str:
    return f"…{secret[-4:]}" if len(secret) >= 8 else "…"


def check_api_key(
    cfg: AppConfig | None,
    *,
    verified_ts: float | None = None,
    config_path: str | Path | None = None,
) -> Check:
    """Is an Anthropic key present in the environment?

    Presence only — proving the key *works* costs a network round trip, so it's
    a separate action. Never returns the key itself; this endpoint is
    unauthenticated on localhost.

    Reads the ``.env`` next to the config file, which is the same one the
    wizard's save action writes to. Looking at the current directory instead
    would report "not saved" immediately after a successful save whenever
    ``--config`` points somewhere else.
    """
    name = cfg.ai.api_key_env if cfg else "ANTHROPIC_API_KEY"
    if config_path is not None:
        load_dotenv_if_present(Path(config_path).resolve().parent / ".env")
    else:
        load_dotenv_if_present()
    value = os.environ.get(name, "").strip()
    if not value:
        return Check(
            id="api_key",
            label="Your AI key",
            state="fail",
            detail="Not saved yet. deal-radar needs this to judge whether a listing is a match.",
            blocking=True,
            fix="Paste an Anthropic API key below — it's stored on this computer only.",
            action="save_api_key",
            extra={"env_var": name},
        )
    detail = f"Saved (ends in {_mask(value)})."
    if verified_ts:
        detail += f" Last checked {_ago(verified_ts)}."
        state = "ok"
    else:
        detail += " We haven't checked it works yet."
        state = "warn"
    return Check(
        id="api_key",
        label="Your AI key",
        state=state,
        detail=detail,
        blocking=True,
        action="test_api_key",
        extra={"env_var": name, "hint": _mask(value)},
    )


def check_facebook(cfg: AppConfig | None, *, checked: dict[str, Any] | None = None) -> Check:
    """Is a Facebook sign-in saved, and does it still look usable?

    An expired session is the worst silent failure in the product: the scraper
    logs a warning, skips the search, and the scan finishes looking clean with
    zero results. So a stale-but-present session is reported as a warning with
    a one-click way to actually verify it.
    """
    mk_cfg = (cfg.marketplaces.get("facebook") if cfg else None) or None
    path = resolve_session_path(mk_cfg) if mk_cfg else Path()
    if mk_cfg is None:
        return Check(
            id="facebook",
            label="Facebook sign-in",
            state="fail",
            detail="Facebook isn't set up as a place to look.",
            blocking=True,
            fix="Add Facebook under 'Where to look' in Settings.",
            action="open_settings",
        )
    info = read_session_state(path)
    if not info["exists"]:
        return Check(
            id="facebook",
            label="Facebook sign-in",
            state="fail",
            detail="You haven't signed in to Facebook yet.",
            blocking=True,
            fix="Sign in once here — a browser window opens and the sign-in is saved.",
            action="facebook_login",
            extra={"path": info["path"]},
        )
    if not info["readable"] or not info["has_login_cookies"]:
        return Check(
            id="facebook",
            label="Facebook sign-in",
            state="fail",
            detail="The saved sign-in looks damaged.",
            blocking=True,
            fix="Sign in again to replace it.",
            action="facebook_login",
            extra={"path": info["path"]},
        )
    if info["expired"]:
        return Check(
            id="facebook",
            label="Facebook sign-in",
            state="fail",
            detail="Your saved sign-in has expired.",
            blocking=True,
            fix="Sign in again — it only takes a moment.",
            action="facebook_login",
            extra={"path": info["path"]},
        )
    saved = _ago(info["saved_ts"]) if info["saved_ts"] else "a while ago"
    if checked and checked.get("ok") and checked.get("ts"):
        return Check(
            id="facebook",
            label="Facebook sign-in",
            state="ok",
            detail=f"Working when we checked it {_ago(float(checked['ts']))}.",
            blocking=False,
            action="facebook_check",
            extra={"path": info["path"]},
        )
    return Check(
        id="facebook",
        label="Facebook sign-in",
        state="warn",
        detail=(
            f"Saved {saved}, but we haven't checked it since. Facebook sign-ins expire, "
            "and an expired one makes scans quietly find nothing."
        ),
        blocking=False,
        fix="Check it now — takes about 20 seconds.",
        action="facebook_check",
        extra={"path": info["path"]},
    )


def check_alerts(cfg: AppConfig | None) -> Check:
    """Is there somewhere to send matches, and is it a kind we can actually send to?"""
    if cfg is None:
        return Check(
            id="alerts",
            label="Where alerts go",
            state="unknown",
            detail="Can't tell until your settings are valid.",
        )
    unsupported = [n.type for n in cfg.notifiers if n.type not in IMPLEMENTED_NOTIFIERS]
    if unsupported:
        kinds = ", ".join(sorted(set(unsupported)))
        return Check(
            id="alerts",
            label="Where alerts go",
            state="fail",
            detail=f"'{kinds}' alerts aren't built yet, so a scan would stop with an error.",
            blocking=True,
            fix="Switch to phone alerts (ntfy) under 'How you get alerted'.",
            action="open_settings",
        )
    topics = [getattr(n, "topic", None) for n in cfg.notifiers]
    named = next((t for t in topics if t), None)
    detail = f'Phone alerts to ntfy topic "{named}".' if named else "Configured."
    return Check(
        id="alerts",
        label="Where alerts go",
        state="ok",
        detail=detail,
        blocking=False,
        action="test_notify",
    )


def check_marketplaces(cfg: AppConfig | None) -> Check | None:
    """Warn about configured marketplaces we have no adapter for."""
    if cfg is None:
        return None
    unsupported = sorted(set(cfg.marketplaces) - set(IMPLEMENTED_MARKETPLACES))
    if not unsupported:
        return None
    kinds = ", ".join(f"'{n}'" for n in unsupported)
    return Check(
        id="marketplaces",
        label="Where to look",
        state="fail",
        detail=f"deal-radar can't search {kinds} yet, so a scan would stop with an error.",
        blocking=True,
        fix="Remove it, or switch it off, under 'Where to look'.",
        action="open_settings",
    )


def _ago(ts: float) -> str:
    """Human-friendly age, e.g. 'just now', '3 hours ago', '37 days ago'."""
    seconds = max(0.0, time.time() - ts)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(round(minutes))} minutes ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(round(hours))} hours ago"
    return f"{int(round(hours / 24))} days ago"


def all_checks(
    config_path: str | Path,
    *,
    key_verified_ts: float | None = None,
    facebook_checked: dict[str, Any] | None = None,
) -> list[Check]:
    """Every readiness check, in the order the wizard should present them."""
    config_check, cfg = check_config(config_path)
    checks = [
        config_check,
        check_browser(),
        check_api_key(cfg, verified_ts=key_verified_ts, config_path=config_path),
        check_facebook(cfg, checked=facebook_checked),
        check_alerts(cfg),
    ]
    marketplaces = check_marketplaces(cfg)
    if marketplaces is not None:
        checks.append(marketplaces)
    return checks


def summarize(checks: list[Check]) -> dict[str, Any]:
    """Roll checks up into the shape ``GET /api/setup/status`` returns."""
    blocking_failures = [c for c in checks if c.blocking and c.state == "fail"]
    warnings = [c for c in checks if c.state == "warn"]
    return {
        "ok": not blocking_failures,
        "setup_required": bool(blocking_failures),
        "blocking_failures": len(blocking_failures),
        "warnings": len(warnings),
        "checks": [c.as_dict() for c in checks],
    }
