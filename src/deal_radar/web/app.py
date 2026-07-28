"""FastAPI app for the local control UI: config editor, matches, live logs, scanner control."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .. import paths
from ..ai.pricing import METER, estimate_eval_cost
from ..config.formvalidate import capability_warnings, cross_field_errors, friendly_message
from ..config.loader import collect_env_refs, missing_env_refs, validate_config_text
from ..config.schema import (
    AIConfig,
    AppConfig,
    MarketplaceConfig,
    MessagingConfig,
    ScanConfig,
    ScheduleConfig,
)
from ..config.starter import random_topic
from ..config.writer import ConfigWriteConflict, build_patched_text, etag_for, write_config
from ..errors import ConfigError
from ..logging import LogBuffer, attach_log_buffer, get_logger
from ..messaging.store import SqliteDraftStore
from . import preflight, setup
from .controller import ScannerController
from .formspec import build_formspec, spec_for
from .runner import build_free_scan_job
from .sender import MessageSender
from .setup import FacebookLogin, SetupError
from .worker import BackgroundJob

log = get_logger("web.app")

# The frontend lives in plain files next to this module rather than in a Python
# string: it needs real HTML/CSS/JS tooling, and serving it from disk lets us
# ship a strict CSP (no inline script, no external origins).
STATIC_DIR = Path(__file__).resolve().parent / "static"

# How often the SSE generator re-reads the ring buffer, and how long it may go
# quiet before sending a comment frame to keep the connection alive.
_SSE_POLL_SECONDS = 0.7
_SSE_HEARTBEAT_SECONDS = 15.0


def _atomic_write(path: Path, text: str) -> None:
    """Replace a file's contents without ever leaving it half-written.

    Also keeps one ``.bak`` generation, so a bad save is recoverable by hand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        with tmp:
            tmp.write(text)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def sse_resume_point(last_event_id: str | None, after: int) -> int:
    """Where a log stream should resume from.

    ``Last-Event-ID`` is what EventSource replays automatically on reconnect
    and wins over the ``after`` query param; a malformed value falls back.
    """
    if last_event_id is not None:
        try:
            return int(last_event_id)
        except ValueError:
            pass
    return after


def sse_frame(seq: int, line: str) -> str:
    """One SSE event. The ``id:`` is what makes reconnects resumable."""
    return f"id: {seq}\ndata: {json.dumps({'seq': seq, 'line': line})}\n\n"


async def sse_log_lines(
    buffer: LogBuffer,
    start: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    poll: float = _SSE_POLL_SECONDS,
    heartbeat: float = _SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Stream buffered log lines as SSE until the client goes away.

    Split out of the endpoint so it can be driven directly in tests: an
    endless generator deadlocks TestClient's teardown.
    """
    last = start
    yield "retry: 3000\n\n"
    idle = 0.0
    while not await is_disconnected():
        for seq, line in buffer.since(last):
            last = seq
            yield sse_frame(seq, line)
            idle = 0.0
        await asyncio.sleep(poll)
        idle += poll
        if idle >= heartbeat:
            idle = 0.0
            yield ": ping\n\n"  # keeps proxies and idle detection from cutting us off


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON request body, treating anything unusable as empty."""
    try:
        body = json.loads((await request.body()) or b"{}")
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _env_warnings(text: str) -> list[dict[str, str]]:
    """Unset ``${VAR}`` references, reported as warnings rather than errors.

    A document shouldn't be un-editable because a runtime secret isn't exported
    in the shell running the UI; ``load_config`` still enforces it at scan time.
    """
    return [
        {
            "kind": "env",
            "message": (
                f"This refers to an environment variable called {name}, which isn't set "
                "on this computer. Scans will fail until it is."
            ),
        }
        for name in missing_env_refs(text)
    ]


def _schema_defaults() -> dict[str, Any]:
    """Defaults for every optional setting, so the form can label the fallbacks.

    Lets a per-item override read "Use my default (4 of 5)" with the real number
    rather than a hardcoded one that could go stale.
    """
    return {
        "ai": AIConfig().model_dump(),
        "schedule": ScheduleConfig().model_dump(),
        "scan": ScanConfig().model_dump(),
        "messaging": MessagingConfig().model_dump(),
        "marketplace": MarketplaceConfig().model_dump(),
        "notify_top_n": AppConfig.model_fields["notify_top_n"].get_default(),
        "version": AppConfig.model_fields["version"].get_default(),
    }


def _effective(cfg: AppConfig | None) -> dict[str, Any]:
    """The values per-item overrides actually fall back to, mirroring the schema helpers."""
    if cfg is None:
        return {"items": []}
    return {
        "items": [
            {
                "name": item.name,
                "min_rating": item.effective_min_rating(cfg.ai),
                "negotiate": item.effective_negotiate(cfg.messaging),
                "offer_percent": item.effective_offer_percent(cfg.messaging),
            }
            for item in cfg.items
        ]
    }


def _check_raw(
    raw: dict[str, Any], text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], AppConfig | None]:
    """Validate a settings mapping the way the form needs it reported.

    Cross-field checks run first so their failures carry a real field location
    (pydantic reports them against the model, with no leaf to highlight), then
    pydantic has the final say. Unset ``${VAR}`` refs are warnings, not errors:
    a document shouldn't be un-editable because a runtime secret isn't exported
    in the shell running the server.
    """
    errors = list(cross_field_errors(raw))
    covered = {tuple(e["loc"][:2]) for e in errors}
    cfg: AppConfig | None = None
    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors(include_url=False):
            loc = list(error["loc"])
            # Drop pydantic's leaf-less duplicate of a check we already scoped.
            if len(loc) == 2 and tuple(loc) in covered:
                continue
            spec = spec_for(_spec_path(loc))
            errors.append(
                {
                    "loc": loc,
                    "type": error["type"],
                    "msg": friendly_message(dict(error), spec["label"] if spec else None),
                    "detail": error["msg"],
                }
            )
    warnings = list(capability_warnings(raw))
    if text:
        warnings.extend(_env_warnings(text))
    return errors, warnings, cfg


def _spec_path(loc: list[Any]) -> str:
    """Turn a pydantic error location into the form's dotted spec path.

    ``("items", 0, "price_max")`` -> ``items.*.price_max``; a discriminated
    union inserts its tag, so ``("notifiers", 1, "ntfy", "topic")`` keeps it.
    """
    return ".".join("*" if isinstance(part, int) else str(part) for part in loc)


def _is_local_origin(origin: str, host: str) -> bool:
    """Is this Origin/Referer our own server?"""
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    # Same host header (including port) means same server.
    return not parsed.netloc or not host or parsed.netloc == host


async def _guard_state_changing_requests(request: Request, call_next: Any) -> Any:
    """Block cross-site writes without adding a password to a personal tool.

    The server has no auth, so before this guard any page the user happened to
    be browsing could POST a form to 127.0.0.1 and overwrite their config or
    start a paid scan — no preflight is required for a simple form POST, and
    the attacker never needs to read the response. Requiring JSON forces a
    CORS preflight, which the browser blocks because we send no CORS headers.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    if origin and not _is_local_origin(origin, host):
        return JSONResponse(
            {"ok": False, "error": "cross-site requests are not allowed"}, status_code=403
        )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse(
            {
                "ok": False,
                "error": "this endpoint requires Content-Type: application/json",
            },
            status_code=415,
        )
    return await call_next(request)


def create_app(
    *,
    config_path: str = "config.yaml",
    controller: ScannerController | None = None,
    log_buffer: LogBuffer | None = None,
    sender: MessageSender | None = None,
    login_fn: Any = None,
) -> FastAPI:
    """Build the web app.

    Inject ``controller``/``log_buffer``/``sender``/``login_fn`` in tests so no
    real browser is ever launched.
    """
    app = FastAPI(title="deal-radar")
    cfg_path = Path(config_path)
    buffer = log_buffer if log_buffer is not None else attach_log_buffer()

    if controller is None:
        from .runner import build_jobs

        loop_job, once_job = build_jobs(str(cfg_path))
        controller = ScannerController(loop_job, once_job)
    ctl: ScannerController = controller  # non-None for use inside the closures below

    if sender is None:
        from .sender import build_send_fn

        sender = MessageSender(
            build_send_fn(str(cfg_path)), lambda: SqliteDraftStore(paths.db_path())
        )
    snd: MessageSender = sender

    # Both drive Playwright, so both run on worker threads and are guarded
    # against overlapping with a scan or a message send.
    fb_login = (
        FacebookLogin(login_fn) if login_fn is not None else FacebookLogin()
    )
    fb_check = BackgroundJob("deal-radar-fb-check")

    app.middleware("http")(_guard_state_changing_requests)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=FileResponse)
    def index() -> FileResponse:
        # no-cache on the shell only: the hashed-by-mtime assets under /static
        # revalidate cheaply, but a stale shell would survive an upgrade.
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={
                "Cache-Control": "no-cache",
                # Blocks every external origin, so the "no CDN, works offline"
                # rule is enforced by the browser rather than by convention.
                "Content-Security-Policy": "default-src 'self'",
            },
        )

    @app.get("/api/config", response_class=PlainTextResponse)
    def get_config() -> str:
        return cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""

    @app.post("/api/config")
    async def save_config(request: Request) -> JSONResponse:
        try:
            body = json.loads((await request.body()) or b"{}")
        except json.JSONDecodeError:
            return JSONResponse(
                {"ok": False, "error": "expected a JSON body like {\"text\": \"...\"}"},
                status_code=400,
            )
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return JSONResponse(
                {"ok": False, "error": "expected a JSON body like {\"text\": \"...\"}"},
                status_code=400,
            )
        text = body["text"]
        try:
            # resolve_env=False: an unset ${VAR} is a runtime concern, reported
            # as a warning below, not a reason to refuse the edit.
            validate_config_text(text, resolve_env=False)
        except ConfigError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),  # full text, never truncated by the client
                    "kind": exc.kind,
                    "errors": exc.errors,
                    "line": exc.line,
                },
                status_code=400,
            )
        _atomic_write(cfg_path, text)
        log.info("config saved via web UI (%d bytes)", len(text))
        return JSONResponse({"ok": True, "warnings": _env_warnings(text)})

    @app.get("/api/config/summary")
    def config_summary() -> dict[str, Any]:
        if not cfg_path.is_file():
            return {"error": "no config file"}
        try:
            cfg = validate_config_text(cfg_path.read_text(encoding="utf-8"))
        except (ConfigError, OSError) as exc:
            return {"error": str(exc)}
        return {
            "items": [
                {
                    "name": item.name,
                    "enabled": item.enabled,
                    "price_min": item.price_min,
                    "price_max": item.price_max,
                    "min_rating": item.effective_min_rating(cfg.ai),
                    "phrases": item.search_phrases,
                }
                for item in cfg.items
            ]
        }

    # --- structured settings form -------------------------------------------------
    #
    # The raw YAML editor stays (under Advanced) and keeps using GET/POST
    # /api/config. These endpoints back the guided form: they read the file as
    # data, and write it back as a minimal patch so comments survive.

    @app.get("/api/config/formspec")
    def config_formspec() -> dict[str, Any]:
        return build_formspec()

    def _raw_config() -> tuple[str, dict[str, Any] | None, ConfigError | None]:
        """The file's text and parsed mapping — env refs left unresolved."""
        if not cfg_path.is_file():
            return "", None, None
        text = cfg_path.read_text(encoding="utf-8")
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            return text, None, ConfigError(
                f"invalid YAML: {exc}",
                kind="yaml",
                line=None if mark is None else mark.line + 1,
            )
        if raw is None:
            return text, {}, None
        if not isinstance(raw, dict):
            return text, None, ConfigError(
                "the top level of the settings file must be a mapping", kind="yaml"
            )
        return text, raw, None

    @app.get("/api/config/form")
    def config_form() -> JSONResponse:
        """Current settings as data, plus everything the form needs to render.

        Always 200, even when the file is missing or invalid: the form has to
        render *something*, and an error page would leave the user stuck.
        """
        text, raw, parse_error = _raw_config()
        if not cfg_path.is_file():
            return JSONResponse(
                {
                    "exists": False,
                    "path": str(cfg_path),
                    "config": None,
                    "etag": None,
                    "valid": False,
                    "errors": [],
                    "warnings": [],
                    "defaults": _schema_defaults(),
                    "effective": {"items": []},
                    "env": {},
                    "starter_topic": random_topic(),
                }
            )
        if raw is None:
            assert parse_error is not None
            return JSONResponse(
                {
                    "exists": True,
                    "path": str(cfg_path),
                    "config": None,
                    "etag": etag_for(text),
                    "valid": False,
                    "kind": parse_error.kind,
                    "line": parse_error.line,
                    "errors": [{"loc": [], "msg": str(parse_error)}],
                    "warnings": [],
                    "defaults": _schema_defaults(),
                    "effective": {"items": []},
                    "env": {},
                }
            )
        errors, warnings, cfg = _check_raw(raw, text)
        return JSONResponse(
            {
                "exists": True,
                "path": str(cfg_path),
                # Raw, not a default-filled model dump: the form needs to tell
                # "not set, uses the default" from "explicitly set to the
                # default", which is what keeps saves minimal.
                "config": raw,
                "etag": etag_for(text),
                "valid": not errors,
                "errors": errors,
                "warnings": warnings,
                "defaults": _schema_defaults(),
                "effective": _effective(cfg),
                # Booleans only — never the values.
                "env": {name: name in os.environ for name in collect_env_refs(text)},
                "starter_topic": random_topic(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/config/validate")
    async def config_validate(request: Request) -> JSONResponse:
        """Check a draft without touching disk; also converts between the views."""
        body = await _json_body(request)
        if isinstance(body.get("text"), str):
            text = body["text"]
            try:
                raw = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                return JSONResponse(
                    {"ok": False, "kind": "yaml", "errors": [{"loc": [], "msg": str(exc)}]}
                )
            if not isinstance(raw, dict):
                return JSONResponse(
                    {
                        "ok": False,
                        "kind": "yaml",
                        "errors": [{"loc": [], "msg": "the top level must be a mapping"}],
                    }
                )
        elif isinstance(body.get("config"), dict):
            raw = body["config"]
            text = ""
        else:
            return JSONResponse(
                {"ok": False, "errors": [{"loc": [], "msg": "send config or text"}]},
                status_code=400,
            )
        errors, warnings, _ = _check_raw(raw, text)
        preview = None
        if not errors and cfg_path.is_file():
            try:
                preview, _ = build_patched_text(cfg_path.read_text(encoding="utf-8"), raw)
            except ConfigError:
                preview = None
        return JSONResponse(
            {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "config": raw,
                "preview_yaml": preview,
            }
        )

    @app.put("/api/config")
    async def config_put(request: Request) -> JSONResponse:
        """Save from the form: a minimal patch, so comments and layout survive."""
        body = await _json_body(request)
        submitted = body.get("config")
        if not isinstance(submitted, dict):
            return JSONResponse(
                {"ok": False, "error": 'expected {"etag": ..., "config": {...}}'},
                status_code=400,
            )
        errors, warnings, _ = _check_raw(submitted, "")
        if errors:
            return JSONResponse(
                {"ok": False, "kind": "schema", "errors": errors, "warnings": warnings},
                status_code=400,
            )
        try:
            result = write_config(
                cfg_path,
                submitted,
                etag=body.get("etag"),
                validate=lambda text: validate_config_text(text, resolve_env=False),
            )
        except ConfigWriteConflict as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "kind": "conflict",
                    "error": str(exc),
                    "current_text": exc.current_text,
                },
                status_code=409,
            )
        except ConfigError as exc:
            return JSONResponse(
                {"ok": False, "kind": exc.kind, "error": str(exc), "errors": exc.errors},
                status_code=400,
            )
        # Paths only, never values: this goes to the live log pane.
        if result["changed"]:
            log.info("settings updated via form: %s", ", ".join(result["changed"]))
        return JSONResponse(
            {
                "ok": True,
                "etag": result["etag"],
                "changed": result["changed"],
                "warnings": warnings,
            }
        )

    @app.get("/api/seen")
    def seen(limit: int = 50) -> dict[str, Any]:
        from ..dedup.sqlite_store import SqliteSeenStore

        db = paths.db_path()
        if not db.is_file():
            return {"rows": []}
        with SqliteSeenStore(db) as store:
            rows = store.list_seen()
        rows.sort(key=lambda r: str(r.get("first_seen_ts") or ""), reverse=True)
        return {"rows": rows[:limit]}

    @app.get("/api/seen/best")
    def seen_best(limit: int = 5) -> dict[str, Any]:
        """Best offers among everything already scanned: match, then rating, then price."""
        from ..dedup.sqlite_store import SqliteSeenStore

        db = paths.db_path()
        if not db.is_file():
            return {"rows": []}
        with SqliteSeenStore(db) as store:
            rows = store.list_seen()

        def rank(r: dict[str, Any]) -> tuple[int, int, float]:
            matched = r.get("matched") or 0
            rating = r.get("rating") or 0
            price = r.get("last_price")
            # Higher match/rating first; among those, the cheapest asking price.
            # Unknown price sorts last rather than pretending to be free.
            cheapest = float(price) if price is not None else float("inf")
            return (-int(matched), -int(rating), cheapest)

        rows.sort(key=rank)
        return {"rows": rows[: max(1, limit)]}

    @app.post("/api/seen/clear")
    def seen_clear(item: str | None = None) -> dict[str, Any]:
        from ..dedup.sqlite_store import SqliteSeenStore

        db = paths.db_path()
        if not db.is_file():
            return {"ok": True, "deleted": 0}
        with SqliteSeenStore(db) as store:
            deleted = store.clear(item)
        scope = f" for {item!r}" if item else ""
        log.info("cleared %d seen listing(s)%s via web UI", deleted, scope)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/seen/delete")
    async def seen_delete(request: Request) -> JSONResponse:
        from ..dedup.sqlite_store import SqliteSeenStore

        try:
            body = json.loads((await request.body()) or b"{}")
        except json.JSONDecodeError:
            body = {}
        item_name = str(body.get("item_name") or "")
        listing_id = str(body.get("listing_id") or "")
        if not item_name or not listing_id:
            return JSONResponse(
                {"ok": False, "error": "item_name and listing_id are required"}, status_code=400
            )
        db = paths.db_path()
        if db.is_file():
            with SqliteSeenStore(db) as store:
                store.delete(item_name, listing_id)
        return JSONResponse({"ok": True})

    @app.get("/api/drafts")
    def drafts(limit: int = 50) -> dict[str, Any]:
        cfg = _cfg_or_none()
        # The UI hides the whole panel when messaging is off: an empty section
        # explaining a feature you haven't turned on is just noise.
        enabled = bool(cfg and cfg.messaging.enabled)
        db = paths.db_path()
        if not db.is_file():
            return {"rows": [], "sending": snd.is_busy(), "messaging_enabled": enabled}
        with SqliteDraftStore(db) as store:
            rows = store.list_drafts(limit=limit)
        return {"rows": rows, "sending": snd.is_busy(), "messaging_enabled": enabled}

    @app.post("/api/drafts/{draft_id}/approve")
    async def approve_draft(draft_id: int, request: Request) -> JSONResponse:
        try:
            body = json.loads((await request.body()) or b"{}")
        except json.JSONDecodeError:
            body = {}
        with SqliteDraftStore(paths.db_path()) as store:
            draft = store.get(draft_id)
            if draft is None:
                return JSONResponse({"ok": False, "error": "unknown draft"}, status_code=404)
            if draft["status"] not in ("pending", "failed"):
                return JSONResponse(
                    {"ok": False, "error": f"draft is {draft['status']}"}, status_code=409
                )
            if snd.is_busy():
                return JSONResponse(
                    {"ok": False, "error": "another send is in progress"}, status_code=409
                )
            text = str(body.get("message") or "").strip() or str(draft["message"])
            store.set_status(draft_id, "sending", message=text)
            draft["message"] = text
        if not snd.send(draft, text):
            with SqliteDraftStore(paths.db_path()) as store:
                store.set_status(draft_id, str(draft["status"]))
            return JSONResponse(
                {"ok": False, "error": "another send is in progress"}, status_code=409
            )
        log.info("draft #%d approved via web UI", draft_id)
        return JSONResponse({"ok": True, "status": snd.status()})

    @app.post("/api/drafts/{draft_id}/dismiss")
    def dismiss_draft(draft_id: int) -> JSONResponse:
        with SqliteDraftStore(paths.db_path()) as store:
            draft = store.get(draft_id)
            if draft is None:
                return JSONResponse({"ok": False, "error": "unknown draft"}, status_code=404)
            if draft["status"] not in ("pending", "failed"):
                return JSONResponse(
                    {"ok": False, "error": f"draft is {draft['status']}"}, status_code=409
                )
            store.set_status(draft_id, "dismissed")
        return JSONResponse({"ok": True})

    # --- setup wizard -----------------------------------------------------------
    #
    # Everything a first-time user needs to get working, without a terminal:
    # what's missing, why it matters, and (where possible) a button to fix it.

    def _cfg_or_none() -> AppConfig | None:
        return setup.load_config_or_none(cfg_path)

    @app.get("/api/setup/status")
    def setup_status() -> JSONResponse:
        state = setup.read_state()
        checks = preflight.all_checks(
            cfg_path,
            key_verified_ts=state.get("key_verified_ts"),
            facebook_checked=(
                {"ok": state.get("fb_checked_ok"), "ts": state.get("fb_checked_ts")}
                if state.get("fb_checked_ts")
                else None
            ),
        )
        body = preflight.summarize(checks)
        body["login"] = fb_login.status()
        body["can_open_browser"] = bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        # The masked key hint lives in here; don't let a proxy or the bfcache
        # hold on to it.
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.post("/api/setup/api-key")
    async def setup_save_key(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            result = setup.save_api_key(cfg_path, _cfg_or_none(), str(body.get("key") or ""))
        except SetupError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.delete("/api/setup/api-key")
    def setup_clear_key() -> JSONResponse:
        return JSONResponse(setup.clear_api_key(cfg_path, _cfg_or_none()))

    @app.post("/api/setup/api-key/test")
    async def setup_test_key(request: Request) -> JSONResponse:
        body = await _json_body(request)
        cfg = _cfg_or_none()
        if cfg is None:
            return JSONResponse(
                {"ok": False, "error": "Fix your settings first."}, status_code=400
            )
        try:
            return JSONResponse(setup.test_api_key(cfg, deep=bool(body.get("deep"))))
        except SetupError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/setup/test-notify")
    def setup_test_notify() -> JSONResponse:
        cfg = _cfg_or_none()
        if cfg is None:
            return JSONResponse(
                {"ok": False, "error": "Fix your settings first."}, status_code=400
            )
        try:
            return JSONResponse(setup.send_test_notification(cfg))
        except SetupError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    def _require_no_browser_in_use() -> JSONResponse | None:
        """Only one Playwright browser at a time — scans, sends and logins share it."""
        if ctl.is_running():
            return JSONResponse(
                {"ok": False, "error": "Stop the scan first — only one browser at a time."},
                status_code=409,
            )
        if snd.is_busy():
            return JSONResponse(
                {"ok": False, "error": "A message is being sent — try again in a moment."},
                status_code=409,
            )
        return None

    @app.post("/api/setup/facebook/check")
    def setup_facebook_check() -> JSONResponse:
        cfg = _cfg_or_none()
        if cfg is None:
            return JSONResponse(
                {"ok": False, "error": "Fix your settings first."}, status_code=400
            )
        busy = _require_no_browser_in_use()
        if busy is not None:
            return busy
        started = fb_check.start(lambda _stop: _run_session_check(cfg))
        if not started:
            return JSONResponse(
                {"ok": False, "error": "Already checking."}, status_code=409
            )
        return JSONResponse({"ok": True, "started": True}, status_code=202)

    def _run_session_check(cfg: AppConfig) -> None:
        app.state.facebook_check_result = setup.check_facebook_session(cfg)

    @app.get("/api/setup/facebook/check")
    def setup_facebook_check_status() -> dict[str, Any]:
        return {
            "busy": fb_check.is_busy(),
            "error": fb_check.error,
            "result": getattr(app.state, "facebook_check_result", None),
        }

    @app.post("/api/setup/facebook/login")
    def setup_facebook_login() -> JSONResponse:
        cfg = _cfg_or_none()
        if cfg is None:
            return JSONResponse(
                {"ok": False, "error": "Fix your settings first."}, status_code=400
            )
        busy = _require_no_browser_in_use()
        if busy is not None:
            return busy
        try:
            fb_login.start(cfg)
        except SetupError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "status": fb_login.status()}, status_code=202)

    @app.get("/api/setup/facebook/login")
    def setup_facebook_login_status() -> dict[str, Any]:
        return fb_login.status()

    @app.post("/api/setup/facebook/login/finish")
    def setup_facebook_login_finish() -> dict[str, Any]:
        fb_login.finish()
        return {"ok": True, "status": fb_login.status()}

    @app.post("/api/setup/facebook/login/cancel")
    def setup_facebook_login_cancel() -> dict[str, Any]:
        fb_login.cancel()
        return {"ok": True, "status": fb_login.status()}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {**ctl.status(), "spend": METER.snapshot()}

    @app.get("/api/pricing")
    def pricing() -> dict[str, Any]:
        """What a scan is likely to cost, so the UI can say so before the click."""
        cfg = _cfg_or_none()
        if cfg is None:
            return {"known": False, "reason": "settings are not valid"}
        per_eval = estimate_eval_cost(cfg.ai)
        enabled = [i for i in cfg.items if i.enabled]
        per_item = cfg.scan.max_evaluations_per_item
        return {
            "known": per_eval is not None,
            "model": cfg.ai.model,
            "per_eval": per_eval,
            "max_evaluations_per_item": per_item,
            "items": len(enabled),
            "max_listings_checked": per_item * len(enabled),
            "max_cost": None if per_eval is None else round(per_eval * per_item * len(enabled), 4),
            "poll_interval_seconds": cfg.schedule.poll_interval_seconds,
            "analyze_images": cfg.ai.analyze_images,
        }

    @app.post("/api/scanner/start")
    def start(mode: str = "loop", free: int = 0) -> dict[str, Any]:
        """Start a scan.

        ``free=1`` is the zero-cost mode: it short-circuits before any AI call
        or detail-page fetch, so it proves the browser and the Facebook sign-in
        work without spending anything. It's the safe first thing to try.
        """
        chosen = mode if mode in ("loop", "once") else "loop"
        if free:
            started = ctl.start_with(build_free_scan_job(str(cfg_path)), mode="free")
        else:
            started = ctl.start(chosen)
        return {"started": started, "status": ctl.status()}

    @app.post("/api/scanner/stop")
    def stop() -> dict[str, Any]:
        ctl.stop()
        return {"ok": True, "status": ctl.status()}

    @app.get("/api/logs")
    def logs(after: int = 0) -> dict[str, Any]:
        pairs = buffer.since(after) if after else buffer.recent()
        return {"lines": [{"seq": s, "line": ln} for s, ln in pairs]}

    @app.get("/api/logs/stream")
    async def logs_stream(request: Request, after: int = 0) -> StreamingResponse:
        # Resume where the client left off. EventSource replays Last-Event-ID
        # automatically on reconnect; without honouring it, every dropped
        # connection re-sent the whole 500-line buffer and the log pane showed
        # everything twice.
        start = sse_resume_point(request.headers.get("Last-Event-ID"), after)
        return StreamingResponse(
            sse_log_lines(buffer, start, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.state.controller = ctl
    app.state.log_buffer = buffer
    app.state.sender = snd
    app.state.facebook_login = fb_login
    app.state.facebook_check = fb_check
    app.state.facebook_check_result = None
    return app
