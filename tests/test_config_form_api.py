"""Tests for the structured settings endpoints (/api/config/form, PUT, /validate)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deal_radar.config.writer import etag_for
from deal_radar.web.app import create_app
from deal_radar.web.controller import ScannerController

RICH_CONFIG = """# my settings
version: 1

ai:
  min_rating: 4   # only strong matches

marketplaces:
  facebook:
    enabled: true
    session_path: null

notifiers:
  - type: ntfy
    topic: my-topic

items:
  - name: "Gaming PC"
    marketplaces: [facebook]
    search_phrases: ["gaming pc"]
    price_min: 1100
    price_max: 2000
    description: >
      A modern desktop.
      DDR5 required.
"""


def _client(
    tmp_path: Path, text: str = RICH_CONFIG, *, create: bool = True
) -> tuple[TestClient, Path]:
    cfg = tmp_path / "config.yaml"
    if create:
        cfg.write_text(text)
    app = create_app(
        config_path=str(cfg),
        controller=ScannerController(lambda s: s.wait(), lambda s: None),
        login_fn=lambda *a, **k: None,
    )
    return TestClient(app), cfg


# --- formspec ------------------------------------------------------------------


def test_formspec_is_served(tmp_path: Path) -> None:
    body = _client(tmp_path)[0].get("/api/config/formspec").json()
    assert [g["id"] for g in body["groups"]][0] == "items"
    assert any(f["path"] == "items.*.description" for f in body["fields"])
    assert body["capabilities"] == {"marketplaces": ["facebook"], "notifiers": ["ntfy"]}


# --- reading -------------------------------------------------------------------


def test_form_returns_raw_values_and_an_etag(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path)
    body = client.get("/api/config/form").json()
    assert body["exists"] is True
    assert body["valid"] is True
    assert body["etag"] == etag_for(cfg.read_text())
    assert body["config"]["items"][0]["name"] == "Gaming PC"
    # Raw, not default-filled: the form must be able to tell "unset" from
    # "explicitly set to the default", which is what keeps saves minimal.
    assert "enabled" not in body["config"]["items"][0]
    assert "scan" not in body["config"]


def test_form_reports_effective_override_values(tmp_path: Path) -> None:
    """So an override can honestly say "Use my default (4)"."""
    body = _client(tmp_path)[0].get("/api/config/form").json()
    assert body["effective"]["items"] == [
        {"name": "Gaming PC", "min_rating": 4, "negotiate": False, "offer_percent": 90}
    ]
    assert body["defaults"]["ai"]["min_rating"] == 4


def test_form_when_there_is_no_file(tmp_path: Path) -> None:
    """Must render something rather than 404 — an error page leaves you stuck."""
    body = _client(tmp_path, create=False)[0].get("/api/config/form").json()
    assert body["exists"] is False
    assert body["config"] is None
    assert body["etag"] is None
    assert body["starter_topic"].startswith("deal-radar-")


def test_form_on_invalid_but_parseable_config(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, "version: 1\nitems: []\nnotifiers: []\n")
    body = client.get("/api/config/form").json()
    assert body["valid"] is False
    assert body["config"] is not None, "the form must still render so it can be fixed"
    assert body["errors"]


def test_form_on_unparseable_yaml(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, "items: [\n")
    body = client.get("/api/config/form").json()
    assert body["config"] is None
    assert body["kind"] == "yaml"
    assert body["line"]


def test_form_never_resolves_env_and_never_leaks_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The highest-severity trap: resolving here would show the user their own
    secret and then write it into the file on the next save."""
    monkeypatch.setenv("MY_TOPIC", "super-secret")
    client, _ = _client(tmp_path, RICH_CONFIG.replace("topic: my-topic", 'topic: "${MY_TOPIC}"'))
    resp = client.get("/api/config/form")
    assert "super-secret" not in resp.text
    body = resp.json()
    assert body["config"]["notifiers"][0]["topic"] == "${MY_TOPIC}"
    assert body["env"] == {"MY_TOPIC": True}  # presence only


def test_form_reports_unset_env_as_present_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MY_TOPIC", raising=False)
    client, _ = _client(tmp_path, RICH_CONFIG.replace("topic: my-topic", 'topic: "${MY_TOPIC}"'))
    body = client.get("/api/config/form").json()
    assert body["env"] == {"MY_TOPIC": False}
    assert body["valid"] is True, "an unset variable is a warning, not an error"
    assert any("MY_TOPIC" in w["message"] for w in body["warnings"])


# --- validating ------------------------------------------------------------------


def test_validate_maps_cross_field_errors_to_a_field(tmp_path: Path) -> None:
    """price_min > price_max is a model-level check in pydantic, so it has no
    field to highlight until we give it one."""
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["items"][0]["price_min"] = 99999
    body = client.post("/api/config/validate", json={"config": config}).json()
    assert body["ok"] is False
    assert body["errors"][0]["loc"] == ["items", 0, "price_max"]
    assert "$99,999" in body["errors"][0]["msg"]


def test_validate_maps_unknown_marketplace_to_the_right_field(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["items"][0]["marketplaces"] = ["kijiji"]
    body = client.post("/api/config/validate", json={"config": config}).json()
    locs = [e["loc"] for e in body["errors"]]
    assert ["items", 0, "marketplaces"] in locs


def test_validate_rewrites_pydantic_errors_for_humans(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["ai"]["min_rating"] = 9
    body = client.post("/api/config/validate", json={"config": config}).json()
    error = next(e for e in body["errors"] if e["loc"] == ["ai", "min_rating"])
    assert error["msg"] == "Only text me when a find scores at least… must be 5 or less."
    assert error["detail"], "the raw pydantic message is kept for the details disclosure"


def test_validate_warns_about_unsupported_notifier(tmp_path: Path) -> None:
    """telegram validates and then raises NotImplementedError mid-scan."""
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["notifiers"] = [{"type": "telegram", "bot_token": "t", "chat_id": "c"}]
    body = client.post("/api/config/validate", json={"config": config}).json()
    assert body["ok"] is True
    assert any("aren't built yet" in w["message"] for w in body["warnings"])


def test_validate_flags_a_duplicate_item_name(tmp_path: Path) -> None:
    """Two items with one name silently share their shown-listings history."""
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["items"].append(dict(config["items"][0]))
    body = client.post("/api/config/validate", json={"config": config}).json()
    assert any(e["loc"] == ["items", 1, "name"] for e in body["errors"])


def test_validate_returns_a_yaml_preview_for_the_advanced_view(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    config = client.get("/api/config/form").json()["config"]
    config["ai"]["min_rating"] = 5
    body = client.post("/api/config/validate", json={"config": config}).json()
    assert "min_rating: 5" in body["preview_yaml"]
    assert "# my settings" in body["preview_yaml"], "the preview keeps comments too"


def test_validate_accepts_raw_text_for_the_reverse_handoff(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    body = client.post("/api/config/validate", json={"text": RICH_CONFIG}).json()
    assert body["ok"] is True
    assert body["config"]["items"][0]["name"] == "Gaming PC"


def test_validate_reports_bad_yaml_text(tmp_path: Path) -> None:
    body = _client(tmp_path)[0].post("/api/config/validate", json={"text": "a: [" }).json()
    assert body["ok"] is False
    assert body["kind"] == "yaml"


def test_validate_touches_nothing_on_disk(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path)
    before = cfg.read_text()
    config = client.get("/api/config/form").json()["config"]
    config["ai"]["min_rating"] = 5
    client.post("/api/config/validate", json={"config": config})
    assert cfg.read_text() == before


# --- saving ---------------------------------------------------------------------


def test_put_saves_a_minimal_patch(tmp_path: Path) -> None:
    """The whole point: a form save must not rewrite a hand-written file."""
    client, cfg = _client(tmp_path)
    form = client.get("/api/config/form").json()
    config = form["config"]
    config["items"][0]["price_max"] = 1500
    resp = client.put("/api/config", json={"etag": form["etag"], "config": config})
    assert resp.status_code == 200
    assert resp.json()["changed"] == ["items.0.price_max"]

    after = cfg.read_text()
    assert "# my settings" in after
    assert "# only strong matches" in after
    assert "description: >" in after
    assert "marketplaces: [facebook]" in after
    assert "session_path: null" in after
    assert len(after.splitlines()) == len(RICH_CONFIG.splitlines())


def test_put_can_add_a_whole_new_block(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path)
    form = client.get("/api/config/form").json()
    config = form["config"]
    config["scan"] = {"max_evaluations_per_item": 5}
    resp = client.put("/api/config", json={"etag": form["etag"], "config": config})
    assert resp.status_code == 200
    assert "max_evaluations_per_item: 5" in cfg.read_text()


def test_put_rejects_invalid_and_leaves_the_file_alone(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path)
    before = cfg.read_text()
    form = client.get("/api/config/form").json()
    config = form["config"]
    config["ai"]["min_rating"] = 99
    resp = client.put("/api/config", json={"etag": form["etag"], "config": config})
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["loc"] == ["ai", "min_rating"]
    assert cfg.read_text() == before


def test_put_detects_a_concurrent_edit(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path)
    form = client.get("/api/config/form").json()
    # Someone edits the file (or the Advanced view saves) in the meantime.
    cfg.write_text(RICH_CONFIG.replace("min_rating: 4", "min_rating: 2"))
    config = form["config"]
    config["ai"]["min_rating"] = 5
    resp = client.put("/api/config", json={"etag": form["etag"], "config": config})
    assert resp.status_code == 409
    body = resp.json()
    assert body["kind"] == "conflict"
    # The current text comes back so unsaved work can be recovered.
    assert "min_rating: 2" in body["current_text"]
    assert "min_rating: 2" in cfg.read_text()


def test_put_with_null_etag_creates_the_file(tmp_path: Path) -> None:
    client, cfg = _client(tmp_path, create=False)
    config = {
        "version": 1,
        "marketplaces": {"facebook": {"enabled": True}},
        "notifiers": [{"type": "ntfy", "topic": "t"}],
        "items": [
            {
                "name": "Bike",
                "marketplaces": ["facebook"],
                "search_phrases": ["bike"],
                "description": "a bike",
            }
        ],
    }
    assert client.put("/api/config", json={"etag": None, "config": config}).status_code == 200
    assert "Bike" in cfg.read_text()


def test_put_requires_a_config_object(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.put("/api/config", json={"etag": None}).status_code == 400


def test_put_logs_paths_but_never_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log pane is on screen; a changed secret must not appear in it."""
    import logging

    client, _ = _client(tmp_path)
    form = client.get("/api/config/form").json()
    config = form["config"]
    config["notifiers"][0]["topic"] = "a-brand-new-secret-topic"
    with caplog.at_level(logging.INFO):
        client.put("/api/config", json={"etag": form["etag"], "config": config})
    assert "notifiers.0.topic" in caplog.text
    assert "a-brand-new-secret-topic" not in caplog.text


def test_the_raw_editor_still_works(tmp_path: Path) -> None:
    """The Advanced escape hatch must keep functioning alongside the form."""
    client, cfg = _client(tmp_path)
    edited = RICH_CONFIG.replace("min_rating: 4", "min_rating: 3")
    assert client.post("/api/config", json={"text": edited}).status_code == 200
    assert "min_rating: 3" in cfg.read_text()
    assert client.get("/api/config").text == cfg.read_text()
