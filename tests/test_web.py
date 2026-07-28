"""Tests for the web UI: ScannerController, log buffer, sender, and FastAPI endpoints."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deal_radar import paths
from deal_radar.dedup.sqlite_store import SqliteSeenStore
from deal_radar.errors import SendError
from deal_radar.logging import LogBuffer
from deal_radar.messaging.store import SqliteDraftStore
from deal_radar.models import Evaluation, Listing
from deal_radar.web.app import create_app, sse_frame, sse_log_lines, sse_resume_point
from deal_radar.web.controller import ScannerController
from deal_radar.web.sender import MessageSender

VALID_CONFIG = """version: 1
ai: {model: claude-haiku-4-5, min_rating: 4}
marketplaces: {facebook: {enabled: true}}
notifiers: [{type: ntfy, topic: t}]
items: [{name: PC, marketplaces: [facebook], search_phrases: [gaming pc], description: d}]
"""


def _wait(pred: object, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


# --- LogBuffer ---------------------------------------------------------------


def test_log_buffer_seq_and_since() -> None:
    buf = LogBuffer(capacity=3)
    buf.append("a")
    buf.append("b")
    assert [ln for _, ln in buf.recent()] == ["a", "b"]
    seq_a = buf.recent()[0][0]
    assert [ln for _, ln in buf.since(seq_a)] == ["b"]  # only newer than a


def test_log_buffer_is_bounded() -> None:
    buf = LogBuffer(capacity=2)
    for c in "abc":
        buf.append(c)
    assert [ln for _, ln in buf.recent()] == ["b", "c"]  # "a" evicted


# --- ScannerController -------------------------------------------------------


def test_controller_start_stop() -> None:
    started = threading.Event()

    def loop_job(stop: threading.Event) -> None:
        started.set()
        stop.wait()

    ctl = ScannerController(loop_job, lambda s: None)
    assert ctl.start("loop") is True
    assert started.wait(1.0)
    assert ctl.is_running()
    assert ctl.start("loop") is False  # already running
    ctl.stop()
    assert _wait(lambda: not ctl.is_running())
    assert ctl.status()["running"] is False


def test_controller_records_error() -> None:
    def boom(stop: threading.Event) -> None:
        raise RuntimeError("kaboom")

    ctl = ScannerController(boom, boom)
    ctl.start("once")
    assert _wait(lambda: not ctl.is_running())
    assert "kaboom" in (ctl.status()["error"] or "")


def test_controller_unknown_mode() -> None:
    ctl = ScannerController(lambda s: None, lambda s: None)
    with pytest.raises(ValueError, match="mode"):
        ctl.start("nope")


# --- FastAPI endpoints -------------------------------------------------------


def _block_until_stop(stop: threading.Event) -> None:
    stop.wait()


def _client(tmp_path: Path) -> tuple[TestClient, Path, LogBuffer, ScannerController]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    buf = LogBuffer()
    ctl = ScannerController(_block_until_stop, lambda s: None)
    app = create_app(config_path=str(cfg), controller=ctl, log_buffer=buf)
    return TestClient(app), cfg, buf, ctl


def test_static_assets_served(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    for name, fragment in (
        ("app.css", "body {"),
        ("main.js", "refreshStatus"),
        ("common.js", "export"),
    ):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, name
        assert fragment in r.text, name


def test_index_sets_no_cache_and_csp(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    # A strict CSP is what mechanically enforces "no CDN, works offline".
    assert r.headers["content-security-policy"] == "default-src 'self'"


def test_static_dir_ships_with_the_package() -> None:
    """Guard against a future .gitignore or build rule swallowing the frontend.

    Without this, a packaging regression would only show up as a blank page
    for someone who installed from a wheel.
    """
    import deal_radar.web

    static = Path(deal_radar.web.__file__).parent / "static"
    for name in ("index.html", "app.css", "common.js", "main.js"):
        asset = static / name
        assert asset.is_file(), f"{name} is missing from the package"
        assert asset.stat().st_size > 0, f"{name} is empty"


def test_frontend_has_no_external_origins() -> None:
    """The page must work offline: no CDN, no remote font, no analytics."""
    import re

    import deal_radar.web

    static = Path(deal_radar.web.__file__).parent / "static"
    pattern = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)
    for asset in static.iterdir():
        assert not pattern.search(asset.read_text()), f"{asset.name} references an external origin"


def test_index_and_get_config(tmp_path: Path) -> None:
    client, cfg, _, _ = _client(tmp_path)
    assert "deal-radar" in client.get("/").text
    assert client.get("/api/config").text == cfg.read_text()


def test_config_summary(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    body = client.get("/api/config/summary").json()
    assert [i["name"] for i in body["items"]] == ["PC"]


def test_save_config_valid_writes_file(tmp_path: Path) -> None:
    client, cfg, _, _ = _client(tmp_path)
    edited = VALID_CONFIG.replace("min_rating: 4", "min_rating: 5")
    resp = client.post("/api/config", json={"text": edited})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert "min_rating: 5" in cfg.read_text()


def test_save_config_invalid_rejected_and_file_unchanged(tmp_path: Path) -> None:
    client, cfg, _, _ = _client(tmp_path)
    before = cfg.read_text()
    resp = client.post("/api/config", json={"text": "{}"})  # missing required sections
    assert resp.status_code == 400 and resp.json()["ok"] is False
    assert cfg.read_text() == before  # not clobbered


def test_save_config_error_is_full_text_not_a_header(tmp_path: Path) -> None:
    """The reported bug: the client used to show only the first line.

    For a schema failure that first line is the content-free header
    "config validation failed for <submitted config>:", so the user was told
    nothing at all. The body must carry the whole message plus per-field
    locations.
    """
    client, _, _, _ = _client(tmp_path)
    bad = "version: 1\nitems: []\nnotifiers: []\n"
    body = client.post("/api/config", json={"text": bad}).json()
    assert body["kind"] == "schema"
    assert body["error"].count("\n") >= 2, "must be more than the header line"
    assert "notifiers" in body["error"] and "items" in body["error"]
    locs = [tuple(e["loc"]) for e in body["errors"]]
    assert ("notifiers",) in locs and ("items",) in locs


def test_save_config_reports_unset_env_as_warning_not_error(tmp_path: Path) -> None:
    """An unset ${VAR} shouldn't make a document un-editable in the browser."""
    client, cfg, _, _ = _client(tmp_path)
    text = VALID_CONFIG.replace("topic: t", 'topic: "${DEAL_RADAR_NOPE}"')
    resp = client.post("/api/config", json={"text": text})
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert any("DEAL_RADAR_NOPE" in w["message"] for w in warnings)
    assert "${DEAL_RADAR_NOPE}" in cfg.read_text()  # stored unresolved


def test_save_config_keeps_a_backup(tmp_path: Path) -> None:
    client, cfg, _, _ = _client(tmp_path)
    original = cfg.read_text()
    edited = VALID_CONFIG.replace("min_rating: 4", "min_rating: 5")
    client.post("/api/config", json={"text": edited})
    assert cfg.with_suffix(".yaml.bak").read_text() == original


def test_save_config_rejects_a_non_json_body(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    resp = client.post("/api/config", content=VALID_CONFIG, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415


def test_form_post_from_another_site_is_blocked(tmp_path: Path) -> None:
    """CSRF: a plain form POST cannot set application/json without a preflight.

    Before the guard, any page the user was browsing could overwrite their
    config or start a paid scan by submitting a hidden form at 127.0.0.1.
    """
    client, cfg, _, _ = _client(tmp_path)
    before = cfg.read_text()
    resp = client.post(
        "/api/config",
        content="text=whatever",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 415
    assert cfg.read_text() == before


def test_cross_origin_json_post_is_blocked(tmp_path: Path) -> None:
    client, _, _, ctl = _client(tmp_path)
    resp = client.post(
        "/api/scanner/start", json={}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert ctl.is_running() is False


def test_same_origin_post_is_allowed(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    resp = client.post(
        "/api/seen/clear",
        json={},
        headers={"Origin": "http://127.0.0.1:8000", "Host": "127.0.0.1:8000"},
    )
    assert resp.status_code == 200


def test_get_requests_are_never_blocked(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 200


def test_sse_resume_point_prefers_last_event_id() -> None:
    assert sse_resume_point("42", 0) == 42
    assert sse_resume_point(None, 7) == 7
    assert sse_resume_point("garbage", 7) == 7  # malformed header falls back


def test_sse_frame_carries_an_id() -> None:
    """The id: line is what makes an EventSource reconnect resumable."""
    frame = sse_frame(9, "hello")
    assert frame.startswith("id: 9\ndata: ")
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {"seq": 9, "line": "hello"}


def test_logs_stream_replays_the_buffer_then_resumes(tmp_path: Path) -> None:
    """Without Last-Event-ID support, every reconnect replayed all 500 lines."""
    buf = LogBuffer()
    buf.append("first")
    buf.append("second")
    seqs = [s for s, _ in buf.recent()]

    async def collect(start: int) -> str:
        calls = {"n": 0}

        async def disconnected() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # one pass, then hang up

        chunks = [
            c async for c in sse_log_lines(buf, start, disconnected, poll=0.0, heartbeat=99)
        ]
        return "".join(chunks)

    full = asyncio.run(collect(0))
    assert "retry: 3000" in full
    assert f"id: {seqs[0]}" in full
    assert "first" in full and "second" in full

    resumed = asyncio.run(collect(seqs[0]))
    assert "first" not in resumed, "a resumed stream must not repeat delivered lines"
    assert "second" in resumed


def test_logs_stream_sends_a_heartbeat_when_idle(tmp_path: Path) -> None:
    """Keeps the connection from being reaped during a long quiet scan."""

    async def collect() -> str:
        calls = {"n": 0}

        async def disconnected() -> bool:
            calls["n"] += 1
            return calls["n"] > 2

        chunks = [
            c
            async for c in sse_log_lines(LogBuffer(), 0, disconnected, poll=1.0, heartbeat=1.0)
        ]
        return "".join(chunks)

    assert ": ping" in asyncio.run(collect())


def test_logs_endpoint_returns_buffer(tmp_path: Path) -> None:
    client, _, buf, _ = _client(tmp_path)
    buf.append("a scan happened")
    lines = [item["line"] for item in client.get("/api/logs").json()["lines"]]
    assert "a scan happened" in lines


def test_scanner_start_stop_endpoints(tmp_path: Path) -> None:
    client, _, _, ctl = _client(tmp_path)
    assert client.get("/api/status").json()["running"] is False
    started = client.post("/api/scanner/start", params={"mode": "loop"}, json={}).json()
    assert started["started"] is True
    assert _wait(ctl.is_running)
    # second start while running is a no-op
    again = client.post("/api/scanner/start", params={"mode": "loop"}, json={}).json()
    assert again["started"] is False
    client.post("/api/scanner/stop", json={})
    assert _wait(lambda: not ctl.is_running())


# --- Seen store endpoints -------------------------------------------------------


def _seen_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEAL_RADAR_DATA_DIR", str(tmp_path / "data"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(_block_until_stop, lambda s: None),
        log_buffer=LogBuffer(),
    )
    return TestClient(app)


def _seed_seen() -> None:
    def li(i: str, price: float) -> Listing:
        return Listing(id=i, marketplace="facebook", title=f"PC {i}", url=f"u/{i}", price=price)

    def ev(match: bool, rating: int) -> Evaluation:
        return Evaluation(match=match, rating=rating, rationale="x", model="m")

    with SqliteSeenStore(paths.db_path()) as store:
        store.mark_seen("PC", li("1", 1500.0), ev(True, 5))
        store.mark_seen("PC", li("2", 1200.0), ev(True, 5))
        store.mark_seen("PC", li("3", 800.0), ev(False, 2))
        store.mark_seen("Bike", li("4", 400.0))


def test_seen_best_ranks_match_then_rating_then_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    rows = client.get("/api/seen/best", params={"limit": 3}).json()["rows"]
    # Two 5/5 matches first, cheaper one ahead; the 2/5 non-match trails.
    assert [r["listing_id"] for r in rows] == ["2", "1", "3"]


def test_seen_delete_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    resp = client.post("/api/seen/delete", json={"item_name": "PC", "listing_id": "1"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    with SqliteSeenStore(paths.db_path()) as store:
        assert not store.is_seen("PC", "1")
        assert store.is_seen("PC", "2")


def test_seen_delete_requires_both_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    assert client.post("/api/seen/delete", json={"item_name": "PC"}).status_code == 400


def test_seen_clear_by_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    resp = client.post("/api/seen/clear", params={"item": "PC"}, json={})
    assert resp.json() == {"ok": True, "deleted": 3}
    with SqliteSeenStore(paths.db_path()) as store:
        assert store.is_seen("Bike", "4")  # other item survives


def test_seen_clear_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    resp = client.post("/api/seen/clear", json={})
    assert resp.json() == {"ok": True, "deleted": 4}
    assert client.get("/api/seen").json()["rows"] == []


# --- MessageSender ------------------------------------------------------------


def _store_factory(tmp_path: Path) -> Callable[[], SqliteDraftStore]:
    return lambda: SqliteDraftStore(tmp_path / "drafts.sqlite3")


def _add_draft(store_factory: Callable[[], SqliteDraftStore]) -> dict[str, Any]:
    listing = Listing(id="1", marketplace="facebook", title="RTX PC", url="u/1", price=500.0)
    with store_factory() as store:
        draft_id = store.add_draft(
            item_name="PC", listing=listing, message="hi there", offer_price=450
        )
        draft = store.get(draft_id)
    assert draft is not None
    return draft


def test_sender_sends_and_marks_sent(tmp_path: Path) -> None:
    factory = _store_factory(tmp_path)
    draft = _add_draft(factory)
    sent: list[tuple[int, str]] = []
    sender = MessageSender(lambda d, text: sent.append((d["id"], text)), factory)
    assert sender.send(draft, "edited text") is True
    assert _wait(lambda: not sender.is_busy())
    assert sent == [(draft["id"], "edited text")]
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "sent" and row["error"] is None


def test_sender_failure_marks_failed(tmp_path: Path) -> None:
    factory = _store_factory(tmp_path)
    draft = _add_draft(factory)

    def boom(d: dict[str, Any], text: str) -> None:
        raise SendError("kaboom")

    sender = MessageSender(boom, factory)
    assert sender.send(draft, "hi") is True
    assert _wait(lambda: not sender.is_busy())
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "failed"
    assert "kaboom" in (row["error"] or "")
    assert "kaboom" in (sender.status()["last_error"] or "")


def test_sender_serializes_sends(tmp_path: Path) -> None:
    factory = _store_factory(tmp_path)
    draft = _add_draft(factory)
    release = threading.Event()
    sender = MessageSender(lambda d, text: release.wait(2.0) and None, factory)
    assert sender.send(draft, "a") is True
    assert sender.is_busy()
    assert sender.send(draft, "b") is False  # one at a time
    assert sender.status()["draft_id"] == draft["id"]
    release.set()
    assert _wait(lambda: not sender.is_busy())


# --- Drafts endpoints -----------------------------------------------------------


def _draft_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    send_fn: Callable[[dict[str, Any], str], None] | None = None,
) -> tuple[TestClient, Callable[[], SqliteDraftStore], MessageSender, list[tuple[int, str]]]:
    monkeypatch.setenv("DEAL_RADAR_DATA_DIR", str(tmp_path / "data"))
    factory = lambda: SqliteDraftStore(paths.db_path())  # noqa: E731 - matches app wiring
    sent: list[tuple[int, str]] = []
    sender = MessageSender(send_fn or (lambda d, text: sent.append((d["id"], text))), factory)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_CONFIG)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(_block_until_stop, lambda s: None),
        log_buffer=LogBuffer(),
        sender=sender,
    )
    return TestClient(app), factory, sender, sent


def test_drafts_empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, _ = _draft_client(tmp_path, monkeypatch)
    # messaging_enabled lets the UI hide the whole panel rather than showing an
    # empty section that explains a feature you haven't switched on.
    assert client.get("/api/drafts").json() == {
        "rows": [],
        "sending": False,
        "messaging_enabled": False,
    }


def test_drafts_list_and_approve_with_edited_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, sender, sent = _draft_client(tmp_path, monkeypatch)
    draft = _add_draft(factory)
    rows = client.get("/api/drafts").json()["rows"]
    assert [r["id"] for r in rows] == [draft["id"]]
    assert rows[0]["offer_price"] == 450

    resp = client.post(f"/api/drafts/{draft['id']}/approve", json={"message": "hi (edited)"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _wait(lambda: not sender.is_busy())
    assert sent == [(draft["id"], "hi (edited)")]
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "sent" and row["message"] == "hi (edited)"


def test_approve_unknown_draft_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, _ = _draft_client(tmp_path, monkeypatch)
    assert client.post("/api/drafts/999/approve", json={}).status_code == 404


def test_approve_wrong_status_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, factory, _, _ = _draft_client(tmp_path, monkeypatch)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/dismiss", json={}).status_code == 200
    assert client.post(f"/api/drafts/{draft['id']}/approve", json={}).status_code == 409


def test_approve_while_busy_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    client, factory, sender, _ = _draft_client(
        tmp_path, monkeypatch, send_fn=lambda d, text: release.wait(2.0) and None
    )
    draft = _add_draft(factory)
    with factory() as store:
        listing2 = Listing(id="2", marketplace="facebook", title="PC 2", url="u/2", price=300.0)
        second_id = store.add_draft(
            item_name="PC", listing=listing2, message="yo", offer_price=None
        )
    assert client.post(f"/api/drafts/{draft['id']}/approve", json={}).status_code == 200
    assert client.post(f"/api/drafts/{second_id}/approve", json={}).status_code == 409
    release.set()
    assert _wait(lambda: not sender.is_busy())


def test_failed_send_records_error_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(d: dict[str, Any], text: str) -> None:
        raise SendError("selector missing")

    client, factory, sender, _ = _draft_client(tmp_path, monkeypatch, send_fn=boom)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/approve", json={}).status_code == 200
    assert _wait(lambda: not sender.is_busy())
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "failed"
    assert "selector missing" in (row["error"] or "")
    # Retrying a failed draft is allowed.
    assert client.post(f"/api/drafts/{draft['id']}/approve", json={}).status_code == 200
    assert _wait(lambda: not sender.is_busy())


def test_dismiss_unknown_draft_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, _ = _draft_client(tmp_path, monkeypatch)
    assert client.post("/api/drafts/999/dismiss", json={}).status_code == 404


def test_dismiss_sets_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, factory, _, _ = _draft_client(tmp_path, monkeypatch)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/dismiss", json={}).status_code == 200
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "dismissed"
    # A dismissed draft can't be dismissed (or approved) again.
    assert client.post(f"/api/drafts/{draft['id']}/dismiss", json={}).status_code == 409
