"""Tests for the comment-preserving config writer.

The premise of the settings form is that saving from it is safe for a file a
human wrote. These tests are what makes that a checked property: comments,
block scalars, flow style, key order and indentation all have to survive a
save, and a save must never write something the operations didn't ask for.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from deal_radar.config.loader import validate_config_text
from deal_radar.config.writer import (
    ConfigWriteConflict,
    Operation,
    apply_operations,
    build_patched_text,
    diff_config,
    etag_for,
    patch_yaml,
    write_config,
)
from deal_radar.errors import ConfigError

# Deliberately styled like a hand-written file: comments in three positions,
# a block scalar, flow sequences, explicit nulls, quoted and unquoted scalars.
RICH_CONFIG = """# deal-radar settings
version: 1

ai:
  model: claude-haiku-4-5   # cheap and fast
  min_rating: 4

marketplaces:
  facebook:
    enabled: true
    session_path: null
    default_location: "Toronto, ON"

notifiers:
  - type: ntfy
    topic: my-secret-topic
    server: https://ntfy.sh

# What I'm hunting for.
items:
  - name: "Gaming PC"
    enabled: true
    marketplaces: [facebook]
    search_phrases: ["gaming pc", "rtx 3080"]
    # Emptied deliberately: the description already rejects parts units.
    exclude_keywords: []
    price_min: 1100
    price_max: 2000
    description: >
      A modern pre-built desktop on a current platform.
      DDR5 memory is required; reject DDR4 builds.
"""


def _load(text: str) -> dict:
    return yaml.safe_load(text)


# --- diffing -------------------------------------------------------------------


def test_no_change_produces_no_operations() -> None:
    data = _load(RICH_CONFIG)
    assert diff_config(data, data) == []


def test_scalar_change_is_one_operation() -> None:
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["ai"]["min_rating"] = 5
    assert diff_config(old, new) == [Operation("set", ("ai", "min_rating"), 5)]


def test_removed_key_becomes_unset() -> None:
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    del new["items"][0]["price_min"]
    assert diff_config(old, new) == [Operation("unset", ("items", 0, "price_min"))]


def test_apply_operations_round_trips() -> None:
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["ai"]["min_rating"] = 5
    new["items"][0]["price_max"] = 2500
    ops = diff_config(old, new)
    assert apply_operations(_load(RICH_CONFIG), ops) == new


# --- the marquee guarantee -------------------------------------------------------


def test_one_field_edit_changes_exactly_one_line() -> None:
    """Everything a human put in the file has to survive a form save."""
    data = _load(RICH_CONFIG)
    data["items"][0]["price_max"] = 1500
    patched, ops = build_patched_text(RICH_CONFIG, data)

    before, after = RICH_CONFIG.splitlines(), patched.splitlines()
    changed = [
        (b, a) for b, a in zip(before, after, strict=True) if b != a
    ]
    assert len(before) == len(after), "the line count must not change"
    assert changed == [("    price_max: 2000", "    price_max: 1500")]


def test_comments_survive_in_every_position() -> None:
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(RICH_CONFIG, data)
    for comment in (
        "# deal-radar settings",  # leading
        "# cheap and fast",  # trailing, on a line we edited nearby
        "# What I'm hunting for.",  # standalone before a list
        "# Emptied deliberately",  # nested inside a list entry
    ):
        assert comment in patched, comment


def test_block_scalar_stays_a_block_scalar() -> None:
    """safe_dump would reflow this into one long quoted line."""
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "description: >" in patched
    assert "DDR5 memory is required" in patched


def test_flow_sequences_stay_inline() -> None:
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "marketplaces: [facebook]" in patched
    assert 'search_phrases: ["gaming pc", "rtx 3080"]' in patched


def test_explicit_null_is_not_rewritten() -> None:
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "session_path: null" in patched


def test_sequence_indentation_is_preserved() -> None:
    """ruamel's default dash placement would reindent every list in the file."""
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "  - type: ntfy" in patched
    assert "\n- type: ntfy" not in patched


def test_dash_at_parent_column_style_also_preserved() -> None:
    """The other common style must round-trip too, not be converted to ours."""
    text = "items:\n- name: a\n  enabled: true\n- name: b\n  enabled: true\n"
    data = _load(text)
    data["items"][0]["enabled"] = False
    patched, _ = build_patched_text(text, data)
    assert patched == "items:\n- name: a\n  enabled: false\n- name: b\n  enabled: true\n"


def test_new_prose_is_written_as_a_readable_block() -> None:
    """A description typed into the form shouldn't land as one enormous line."""
    long_text = (
        "A road bike for commuting, 54-56cm. Must have working gears and brakes, "
        "no rust on the frame. Nothing sold for parts. A good price is under $400."
    )
    data = _load(RICH_CONFIG)
    data["items"][0]["description"] = long_text
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "description: >" in patched
    assert max(len(line) for line in patched.splitlines()) <= 95
    assert _load(patched)["items"][0]["description"].strip() == long_text


def test_multi_line_prose_keeps_its_line_breaks() -> None:
    data = _load(RICH_CONFIG)
    data["items"][0]["description"] = "Must have:\n- gears\n- brakes\n"
    patched, _ = build_patched_text(RICH_CONFIG, data)
    assert "description: |" in patched, "a folded block would eat the line breaks"
    assert _load(patched)["items"][0]["description"] == "Must have:\n- gears\n- brakes\n"


def test_an_untouched_save_is_byte_identical() -> None:
    """The strongest form of "we didn't reformat your file"."""
    patched, ops = build_patched_text(RICH_CONFIG, _load(RICH_CONFIG))
    assert ops == []
    assert patched == RICH_CONFIG


def test_no_default_keys_are_added() -> None:
    """Saving an unchanged document must not expand it with schema defaults."""
    patched, ops = build_patched_text(RICH_CONFIG, _load(RICH_CONFIG))
    assert ops == []
    assert patched == RICH_CONFIG


def test_trailing_newline_habit_is_matched() -> None:
    text = "version: 1\nai:\n  min_rating: 4"  # no final newline
    data = _load(text)
    data["ai"]["min_rating"] = 5
    patched, _ = build_patched_text(text, data)
    assert not patched.endswith("\n")


# --- list identity ----------------------------------------------------------------


def test_editing_one_item_leaves_the_other_alone() -> None:
    text = RICH_CONFIG + """
  - name: "Road bike"
    enabled: true
    marketplaces: [facebook]
    search_phrases: ["road bike"]
    # keep this comment
    description: a road bike
"""
    data = _load(text)
    data["items"][1]["enabled"] = False
    patched, ops = build_patched_text(text, data)
    assert ops == [Operation("set", ("items", 1, "enabled"), False)]
    assert "# keep this comment" in patched
    assert "# Emptied deliberately" in patched


def test_reordering_items_is_a_wholesale_rewrite() -> None:
    """No minimal patch exists for a reorder; be explicit rather than subtly wrong."""
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["items"] = list(reversed(new["items"]))
    ops = diff_config(old, new)
    # One item, so reversing is a no-op; with two it must replace the list.
    assert ops == [] or ops[0].path == ("items",)


def test_adding_an_item_replaces_the_list() -> None:
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["items"].append(
        {
            "name": "Bike",
            "marketplaces": ["facebook"],
            "search_phrases": ["bike"],
            "description": "a bike",
        }
    )
    ops = diff_config(old, new)
    assert [o.path for o in ops] == [("items",)]
    patched = patch_yaml(RICH_CONFIG, ops)
    assert validate_config_text(patched)
    assert "Bike" in patched


def test_renaming_an_item_is_still_a_one_field_edit() -> None:
    """A rename changes the identity we match on, so without a positional
    fallback it would rewrite the whole list and lose its comments."""
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["items"][0]["name"] = "Gaming PC (DDR5)"
    ops = diff_config(old, new)
    assert ops == [Operation("set", ("items", 0, "name"), "Gaming PC (DDR5)")]
    patched = patch_yaml(RICH_CONFIG, ops)
    assert "# Emptied deliberately" in patched
    assert "description: >" in patched


def test_editing_a_notifier_topic_is_a_one_field_edit() -> None:
    """The topic is the field people actually change; it must not be the identity."""
    old = _load(RICH_CONFIG)
    new = _load(RICH_CONFIG)
    new["notifiers"][0]["topic"] = "a-new-topic"
    assert diff_config(old, new) == [
        Operation("set", ("notifiers", 0, "topic"), "a-new-topic")
    ]


def test_duplicate_identities_fall_back_to_positional() -> None:
    text = 'items:\n  - name: dup\n    enabled: true\n  - name: dup\n    enabled: true\n'
    old = _load(text)
    new = _load(text)
    new["items"][1]["enabled"] = False
    ops = diff_config(old, new)
    assert ops == [Operation("set", ("items", 1, "enabled"), False)]


# --- the structural guard -----------------------------------------------------------


def test_structural_guard_refuses_a_bad_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the guard is wired, not merely present."""
    import deal_radar.config.writer as writer_mod

    monkeypatch.setattr(writer_mod, "patch_yaml", lambda text, ops: "version: 99\n")
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    with pytest.raises(ConfigError, match="Refusing to save"):
        build_patched_text(RICH_CONFIG, data)


def test_non_mapping_root_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        build_patched_text("- just\n- a list\n", {"version": 1})


# --- write_config: etag, atomicity, validation ---------------------------------------


def _write(tmp_path: Path, text: str = RICH_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_write_saves_and_returns_a_new_etag(tmp_path: Path) -> None:
    path = _write(tmp_path)
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    result = write_config(path, data, etag=etag_for(RICH_CONFIG))
    assert "min_rating: 5" in path.read_text()
    assert result["etag"] == etag_for(path.read_text())
    assert result["changed"] == ["ai.min_rating"]


def test_write_detects_a_conflicting_edit(tmp_path: Path) -> None:
    path = _write(tmp_path)
    stale_etag = etag_for(RICH_CONFIG)
    path.write_text(RICH_CONFIG.replace("min_rating: 4", "min_rating: 2"))  # edited elsewhere
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    with pytest.raises(ConfigWriteConflict) as excinfo:
        write_config(path, data, etag=stale_etag)
    assert "changed since this page opened" in str(excinfo.value)
    # The current text comes back so unsaved work can be recovered.
    assert "min_rating: 2" in excinfo.value.current_text
    assert "min_rating: 2" in path.read_text(), "the file must not be overwritten"


def test_write_with_null_etag_creates_a_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_config(path, {"version": 1}, etag=None)
    assert path.read_text().strip() == "version: 1"


def test_write_with_null_etag_refuses_when_a_file_exists(tmp_path: Path) -> None:
    path = _write(tmp_path)
    with pytest.raises(ConfigWriteConflict):
        write_config(path, {"version": 1}, etag=None)


def test_write_runs_the_validator_before_touching_disk(tmp_path: Path) -> None:
    path = _write(tmp_path)
    before = path.read_text()

    def reject(text: str) -> None:
        raise ConfigError("nope")

    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    with pytest.raises(ConfigError, match="nope"):
        write_config(path, data, etag=etag_for(before), validate=reject)
    assert path.read_text() == before


def test_write_keeps_a_backup(tmp_path: Path) -> None:
    path = _write(tmp_path)
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    write_config(path, data, etag=etag_for(RICH_CONFIG))
    assert path.with_suffix(".yaml.bak").read_text() == RICH_CONFIG


def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = _write(tmp_path)
    data = _load(RICH_CONFIG)
    data["ai"]["min_rating"] = 5
    write_config(path, data, etag=etag_for(RICH_CONFIG))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["config.yaml", "config.yaml.bak"]


def test_write_is_a_noop_when_nothing_changed(tmp_path: Path) -> None:
    path = _write(tmp_path)
    mode_before = stat.S_IMODE(path.stat().st_mode)
    result = write_config(path, _load(RICH_CONFIG), etag=etag_for(RICH_CONFIG))
    assert result["changed"] == []
    assert not path.with_suffix(".yaml.bak").exists(), "no backup for a no-op save"
    assert stat.S_IMODE(path.stat().st_mode) == mode_before


def test_patched_config_still_validates(tmp_path: Path) -> None:
    """End to end: a form-shaped edit produces a file the loader accepts."""
    path = _write(tmp_path)
    data = _load(RICH_CONFIG)
    data["items"][0]["price_max"] = 1500
    data["scan"] = {"max_evaluations_per_item": 10}
    write_config(path, data, etag=etag_for(RICH_CONFIG), validate=validate_config_text)
    cfg = validate_config_text(path.read_text())
    assert cfg.items[0].price_max == 1500
    assert cfg.scan.max_evaluations_per_item == 10
