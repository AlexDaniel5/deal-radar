"""Tests for the Phase 5 web UI: ScannerController, log buffer, and FastAPI endpoints."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deal_radar.logging import LogBuffer
from deal_radar.web.app import create_app
from deal_radar.web.controller import ScannerController

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
