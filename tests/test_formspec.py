"""Tests for the settings form spec.

The important one is `test_spec_covers_every_schema_field`: the user was
promised that nothing they can express in YAML is lost when the form becomes
the default editor. That is only true if every schema field has a form entry,
so it's asserted rather than assumed — and it fails the day someone adds a
setting without writing copy for it.
"""

from __future__ import annotations

import re

from annotated_types import Ge, Le
from pydantic.fields import FieldInfo

from deal_radar.ai.pricing import SUPPORTED_MODELS
from deal_radar.config.schema import AppConfig
from deal_radar.web.formspec import (
    FIELDS,
    GROUPS,
    MODEL_PREFIXES,
    NESTED_CONTAINERS,
    bounds_for,
    build_formspec,
    spec_for,
)


def _schema_paths() -> set[str]:
    """Every leaf setting in the config schema, as a dotted form path."""
    paths: set[str] = set()
    for model, prefix in MODEL_PREFIXES:
        for name in model.model_fields:
            # Only AppConfig's containers hold other models; ItemConfig
            # .marketplaces is a plain list of names and *is* a form field.
            if model is AppConfig and name in NESTED_CONTAINERS:
                continue
            paths.add(f"{prefix}.{name}" if prefix else name)
    return paths


def test_spec_covers_every_schema_field() -> None:
    """Nothing expressible in the YAML may be missing from the form."""
    spec_paths = {f["path"] for f in FIELDS}
    missing = _schema_paths() - spec_paths
    assert not missing, f"schema fields with no form entry: {sorted(missing)}"


def test_spec_has_no_fields_that_do_not_exist() -> None:
    extra = {f["path"] for f in FIELDS} - _schema_paths()
    assert not extra, f"form entries for non-existent settings: {sorted(extra)}"


def test_no_duplicate_entries() -> None:
    paths = [f["path"] for f in FIELDS]
    assert len(paths) == len(set(paths)), "a field is described twice"


def test_every_field_has_a_label_and_a_group() -> None:
    group_ids = {g["id"] for g in GROUPS}
    for field in FIELDS:
        assert field["label"].strip(), field["path"]
        assert field["group"] in group_ids, f"{field['path']} -> {field['group']}"


def test_every_visible_field_explains_itself() -> None:
    """Advanced fields may be terse; the ones a normal user sees may not be.

    Two exceptions are structural: the notifier `type` selector is rendered as
    the card's own kind-picker, and carries no help text of its own.
    """
    for field in FIELDS:
        if field["advanced"] or field["path"].endswith(".type"):
            continue
        assert field["help"].strip(), f"{field['path']} has no help text"


def test_bounds_come_from_pydantic_not_from_hand_typing() -> None:
    spec = {f["path"]: f for f in build_formspec()["fields"]}
    assert spec["ai.min_rating"]["min"] == 1
    assert spec["ai.min_rating"]["max"] == 5
    assert spec["schedule.poll_interval_seconds"]["min"] == 300
    assert spec["messaging.offer_percent"]["min"] == 50
    assert spec["messaging.offer_percent"]["max"] == 100
    assert spec["scan.max_evaluations_per_item"]["min"] == 0


def test_bounds_match_the_schema_exactly() -> None:
    """A hand-typed bound that drifts is worse than none: the browser would
    accept something the server then rejects."""
    spec = {f["path"]: f for f in build_formspec()["fields"]}
    for model, prefix in MODEL_PREFIXES:
        for name, info in model.model_fields.items():
            if model is AppConfig and name in NESTED_CONTAINERS:
                continue
            path = f"{prefix}.{name}" if prefix else name
            expected = bounds_for(info)
            for key in ("min", "max"):
                assert spec[path].get(key) == expected.get(key), f"{path}.{key}"


def test_bounds_for_reads_ge_and_le() -> None:
    from typing import Annotated

    info = FieldInfo.from_annotation(Annotated[int, Ge(2), Le(9)])
    assert bounds_for(info)["min"] == 2
    assert bounds_for(info)["max"] == 9


def test_required_fields_are_marked() -> None:
    spec = {f["path"]: f for f in build_formspec()["fields"]}
    assert spec["items.*.name"]["required"] is True
    assert spec["items.*.description"]["required"] is True
    assert spec["notifiers.*.ntfy.topic"]["required"] is True
    assert spec["items.*.price_max"]["required"] is False


def test_defaults_are_json_serialisable() -> None:
    import json

    json.dumps(build_formspec())  # would raise on a PydanticUndefined etc.


def test_the_description_field_is_prominent_and_has_an_example() -> None:
    """It's the single most important input: it's what the AI is given."""
    field = spec_for("items.*.description")
    assert field is not None
    assert field["widget"] == "textarea"
    assert field["rows"] and field["rows"] >= 8
    assert field["example"], "a good example is the fastest way to a good description"
    assert not field["advanced"]


def test_keyword_fields_say_which_one_is_the_hard_filter() -> None:
    """Confusing these two is the most likely way to silently miss listings."""
    include = spec_for("items.*.include_keywords")
    exclude = spec_for("items.*.exclude_keywords")
    assert include is not None and exclude is not None
    assert "NOT a requirements list" in include["help"]
    assert "only hard filter" in exclude["help"]


def test_per_item_overrides_name_the_global_they_fall_back_to() -> None:
    for path, target in (
        ("items.*.min_rating", "ai.min_rating"),
        ("items.*.negotiate", "messaging.negotiate"),
        ("items.*.offer_percent", "messaging.offer_percent"),
    ):
        field = spec_for(path)
        assert field is not None
        assert field["overridable"] == target


def test_messaging_carries_the_tos_caution() -> None:
    field = spec_for("messaging.enabled")
    assert field is not None
    assert "terms of service" in (field.get("warning") or "")


def test_model_picker_only_offers_structured_output_models() -> None:
    """Offering one the evaluator can't use hands the user a config that fails
    on its first evaluation."""
    field = spec_for("ai.model")
    assert field is not None
    offered = {o["value"] for o in field["options"]}
    assert offered == {c.id for c in SUPPORTED_MODELS}
    assert "claude-sonnet-4-6" not in offered


def test_expert_fields_are_behind_advanced() -> None:
    for path in (
        "version",
        "ai.provider",
        "ai.max_tokens",
        "ai.api_key_env",
        "marketplaces.*.session_path",
        "schedule.per_request_min_interval_seconds",
    ):
        field = spec_for(path)
        assert field is not None and field["advanced"] is True, path


def test_no_jargon_in_the_words_a_normal_user_reads() -> None:
    """The whole premise of the rework. Enforced so it can't rot."""
    banned = re.compile(
        r"\b(dedup|max[_-]evals|min_rating|notify_top_n|SSE|headless|headful|"
        r"storage_state|dry[_-]run|YAML|regex|boolean|env var|param|stdout|"
        r"NotImplementedError|pydantic)\b",
        re.IGNORECASE,
    )
    for field in FIELDS:
        if field["advanced"]:
            continue  # advanced copy may name the real setting
        for key in ("label", "help"):
            found = banned.search(field.get(key) or "")
            assert not found, f"{field['path']}.{key} says {found.group(0)!r}"


def test_capabilities_are_reported_so_the_ui_can_warn() -> None:
    caps = build_formspec()["capabilities"]
    assert caps["marketplaces"] == ["facebook"]
    assert caps["notifiers"] == ["ntfy"]


def test_every_group_is_used() -> None:
    used = {f["group"] for f in FIELDS}
    assert used == {g["id"] for g in GROUPS}


def test_app_config_has_no_unknown_top_level_fields() -> None:
    """A tripwire: adding a setting to AppConfig without a form entry fails here."""
    covered = {f["path"] for f in FIELDS}
    for name in AppConfig.model_fields:
        if name in NESTED_CONTAINERS:
            continue
        assert name in covered, f"AppConfig.{name} has no form entry"
