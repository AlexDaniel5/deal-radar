"""Tests for the web UI: ScannerController, log buffer, sender, and FastAPI endpoints."""

from __future__ import annotations

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
from deal_radar.web.app import create_app
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
    resp = client.post("/api/config", content=edited)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert "min_rating: 5" in cfg.read_text()


def test_save_config_invalid_rejected_and_file_unchanged(tmp_path: Path) -> None:
    client, cfg, _, _ = _client(tmp_path)
    before = cfg.read_text()
    resp = client.post("/api/config", content="{}")  # missing required sections
    assert resp.status_code == 400 and resp.json()["ok"] is False
    assert cfg.read_text() == before  # not clobbered


def test_logs_endpoint_returns_buffer(tmp_path: Path) -> None:
    client, _, buf, _ = _client(tmp_path)
    buf.append("a scan happened")
    lines = [item["line"] for item in client.get("/api/logs").json()["lines"]]
    assert "a scan happened" in lines


def test_scanner_start_stop_endpoints(tmp_path: Path) -> None:
    client, _, _, ctl = _client(tmp_path)
    assert client.get("/api/status").json()["running"] is False
    started = client.post("/api/scanner/start", params={"mode": "loop"}).json()
    assert started["started"] is True
    assert _wait(ctl.is_running)
    # second start while running is a no-op
    assert client.post("/api/scanner/start", params={"mode": "loop"}).json()["started"] is False
    client.post("/api/scanner/stop")
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

    with SqliteSeenStore(paths.db_path()) as store:
        store.mark_seen("PC", li("1", 1500.0), Evaluation(match=True, rating=5, rationale="x", model="m"))
        store.mark_seen("PC", li("2", 1200.0), Evaluation(match=True, rating=5, rationale="x", model="m"))
        store.mark_seen("PC", li("3", 800.0), Evaluation(match=False, rating=2, rationale="x", model="m"))
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
    resp = client.post("/api/seen/clear", params={"item": "PC"})
    assert resp.json() == {"ok": True, "deleted": 3}
    with SqliteSeenStore(paths.db_path()) as store:
        assert store.is_seen("Bike", "4")  # other item survives


def test_seen_clear_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _seen_client(tmp_path, monkeypatch)
    _seed_seen()
    resp = client.post("/api/seen/clear")
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
    assert client.get("/api/drafts").json() == {"rows": [], "sending": False}


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
    assert client.post("/api/drafts/999/approve").status_code == 404


def test_approve_wrong_status_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, factory, _, _ = _draft_client(tmp_path, monkeypatch)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/dismiss").status_code == 200
    assert client.post(f"/api/drafts/{draft['id']}/approve").status_code == 409


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
    assert client.post(f"/api/drafts/{draft['id']}/approve").status_code == 200
    assert client.post(f"/api/drafts/{second_id}/approve").status_code == 409
    release.set()
    assert _wait(lambda: not sender.is_busy())


def test_failed_send_records_error_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(d: dict[str, Any], text: str) -> None:
        raise SendError("selector missing")

    client, factory, sender, _ = _draft_client(tmp_path, monkeypatch, send_fn=boom)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/approve").status_code == 200
    assert _wait(lambda: not sender.is_busy())
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "failed"
    assert "selector missing" in (row["error"] or "")
    # Retrying a failed draft is allowed.
    assert client.post(f"/api/drafts/{draft['id']}/approve").status_code == 200
    assert _wait(lambda: not sender.is_busy())


def test_dismiss_unknown_draft_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, _ = _draft_client(tmp_path, monkeypatch)
    assert client.post("/api/drafts/999/dismiss").status_code == 404


def test_dismiss_sets_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, factory, _, _ = _draft_client(tmp_path, monkeypatch)
    draft = _add_draft(factory)
    assert client.post(f"/api/drafts/{draft['id']}/dismiss").status_code == 200
    with factory() as store:
        row = store.get(draft["id"])
    assert row is not None and row["status"] == "dismissed"
    # A dismissed draft can't be dismissed (or approved) again.
    assert client.post(f"/api/drafts/{draft['id']}/dismiss").status_code == 409
