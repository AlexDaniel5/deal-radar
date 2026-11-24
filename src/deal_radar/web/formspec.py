"""What the settings form looks like, and what every field is called in English.

The spec lives in Python rather than in the JavaScript so that all this copy is
unit-testable - in particular, a test walks the pydantic models and asserts that
every schema field appears here exactly once. That is what makes "the form can
express everything the YAML can" a checked property rather than a hope, and it
fails loudly the day someone adds a setting without writing words for it.

Numeric bounds are read out of the pydantic ``FieldInfo`` rather than retyped,
so in-browser validation cannot drift from what the server will accept.
"""

from __future__ import annotations

from typing import Any

from annotated_types import Ge, Le, MinLen
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ..ai.pricing import SUPPORTED_MODELS
from ..config.schema import (
    AIConfig,
    AppConfig,
    ItemConfig,
    MarketplaceConfig,
    MessagingConfig,
    NtfyNotifierConfig,
    ScanConfig,
    ScheduleConfig,
    TelegramNotifierConfig,
)
from ..config.starter import EXAMPLE_DESCRIPTION
from ..marketplaces.registry import IMPLEMENTED_MARKETPLACES
from ..notifiers.registry import IMPLEMENTED_NOTIFIERS

__all__ = [
    "FIELDS",
    "GROUPS",
    "MODEL_PREFIXES",
    "NESTED_CONTAINERS",
    "bounds_for",
    "build_formspec",
    "schema_field_infos",
    "spec_for",
]

# The models the form covers, and the dotted prefix each one's fields live under.
# "" means the top level; list entries use a `*` element placeholder.
# AppConfig fields that hold one of the models below rather than a value of
# their own. Scoped to AppConfig deliberately: ItemConfig.marketplaces is a
# plain list of strings and *is* a form field, despite sharing the name.
NESTED_CONTAINERS: frozenset[str] = frozenset(
    {"ai", "schedule", "scan", "messaging", "marketplaces", "notifiers", "items"}
)

MODEL_PREFIXES: tuple[tuple[type[BaseModel], str], ...] = (
    (AppConfig, ""),
    (AIConfig, "ai"),
    (ScheduleConfig, "schedule"),
    (ScanConfig, "scan"),
    (MessagingConfig, "messaging"),
    (MarketplaceConfig, "marketplaces.*"),
    (ItemConfig, "items.*"),
    (NtfyNotifierConfig, "notifiers.*.ntfy"),
    (TelegramNotifierConfig, "notifiers.*.telegram"),
)

GROUPS: tuple[dict[str, str], ...] = (
    {
        "id": "items",
        "title": "Hunting preferences",
        "blurb": "What deal-radar looks for, and what the AI should count as a match.",
    },
    {
        "id": "alerts",
        "title": "Alert preferences",
        "blurb": "How good a listing has to be before you hear about it.",
    },
    {
        "id": "notifications",
        "title": "Notifications",
        "blurb": "Where your alerts are delivered.",
    },
    {
        "id": "where",
        "title": "Search locations",
        "blurb": "The marketplaces to search, and the area you normally search in.",
    },
    {
        "id": "when",
        "title": "Scanning frequency",
        "blurb": "How often deal-radar checks, and how much it does each time.",
    },
    {
        "id": "messaging",
        "title": "Seller messaging",
        "blurb": "Nothing is sent without your approval.",
    },
    {
        "id": "advanced",
        "title": "Advanced",
        "blurb": "Rarely needed.",
    },
)


def _f(
    path: str,
    label: str,
    *,
    group: str,
    widget: str = "text",
    help: str = "",
    placeholder: str | None = None,
    options: list[dict[str, Any]] | None = None,
    unit: str | None = None,
    prefix: str | None = None,
    captions: list[str] | None = None,
    advanced: bool = False,
    rows: int | None = None,
    example: str | None = None,
    warning: str | None = None,
    overridable: str | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "path": path,
        "label": label,
        "group": "advanced" if advanced else group,
        "widget": widget,
        "help": help,
        "advanced": advanced,
    }
    for key, value in (
        ("placeholder", placeholder),
        ("options", options),
        ("unit", unit),
        ("prefix", prefix),
        ("captions", captions),
        ("rows", rows),
        ("example", example),
        ("warning", warning),
        # Which global setting this per-item field falls back to when unset.
        ("overridable", overridable),
        # A sub-heading within a card; purely presentational.
        ("section", section),
    ):
        if value is not None:
            spec[key] = value
    return spec


_MODEL_OPTIONS = [
    {"value": c.id, "label": f"{c.label} - {c.note}"} for c in SUPPORTED_MODELS
]

_MINUTE_OPTIONS = [
    {"value": 600, "label": "10 minutes"},
    {"value": 900, "label": "15 minutes"},
    {"value": 1800, "label": "30 minutes"},
    {"value": 3600, "label": "1 hour"},
    {"value": 7200, "label": "2 hours"},
    {"value": 14400, "label": "4 hours"},
]

FIELDS: tuple[dict[str, Any], ...] = (
    # --- items -----------------------------------------------------------------
    _f(
        "items.*.name",
        "Name",
        group="items",
        section="Basics",
        help="Shown on your alerts.",
        placeholder="Road bike",
    ),
    _f(
        "items.*.enabled",
        "Actively hunting",
        group="items",
        section="Basics",
        widget="toggle",
        help="Off pauses it.",
    ),
    _f(
        "items.*.description",
        "What you want (the AI reads this)",
        group="items",
        section="Basics",
        widget="textarea",
        rows=5,
        example=EXAMPLE_DESCRIPTION,
        help=(
            "The AI judges every listing against this. Cover must-haves, "
            "nice-to-haves, deal-breakers and a good price."
        ),
    ),
    _f(
        "items.*.search_phrases",
        "Search terms",
        group="items",
        section="Keywords",
        widget="chips",
        help="Each runs as its own search. 3-6 broad terms.",
        placeholder="road bike",
    ),
    _f(
        "items.*.include_keywords",
        "Related words",
        group="items",
        section="Keywords",
        widget="chips",
        help=(
            "NOT a requirements list - titles must share a word with these or the "
            "search terms. Keep it broad."
        ),
    ),
    _f(
        "items.*.exclude_keywords",
        "Exclude listings with",
        group="items",
        section="Keywords",
        widget="chips",
        help="The only hard filter: any match drops the listing.",
    ),
    _f(
        "items.*.price_min",
        "Lowest price",
        group="items",
        section="Price and area",
        widget="money",
        prefix="$",
        help="Blank = no limit.",
    ),
    _f(
        "items.*.price_max",
        "Highest price",
        group="items",
        section="Price and area",
        widget="money",
        prefix="$",
        help="Blank = no limit.",
    ),
    _f(
        "items.*.location",
        "City to search",
        group="items",
        section="Price and area",
        help="Blank = your default city.",
    ),
    _f(
        "items.*.radius_km",
        "Radius",
        group="items",
        section="Price and area",
        widget="number",
        unit="km",
        help="Blank = your default distance.",
    ),
    _f(
        "items.*.marketplaces",
        "Search on",
        group="items",
        section="Price and area",
        widget="marketplace-checkboxes",
        help="Only places you've set up.",
    ),
    _f(
        "items.*.min_rating",
        "Minimum score",
        group="items",
        section="Overrides for this hunt",
        widget="rating",
        overridable="ai.min_rating",
        help="Overrides the global setting.",
    ),
    _f(
        "items.*.negotiate",
        "Open with an offer",
        group="items",
        section="Overrides for this hunt",
        widget="toggle",
        overridable="messaging.negotiate",
        help="Overrides the global setting.",
    ),
    _f(
        "items.*.offer_percent",
        "Offer % of asking",
        group="items",
        section="Overrides for this hunt",
        widget="percent",
        overridable="messaging.offer_percent",
        help="Overrides the global setting.",
    ),
    # --- alerts ------------------------------------------------------------------
    _f(
        "ai.min_rating",
        "Minimum score",
        group="alerts",
        widget="rating",
        captions=[
            "1 - anything, very noisy",
            "2 - loosely related",
            "3 - a decent match",
            "4 - a strong match at a good price (recommended)",
            "5 - only exceptional finds",
        ],
        help="Every listing is scored out of 5.",
    ),
    _f(
        "notify_top_n",
        "Alerts per check",
        group="alerts",
        widget="number",
        help="Best matches per check, sent as one message.",
    ),
    _f(
        "ai.analyze_images",
        "AI looks at photos",
        group="alerts",
        widget="toggle",
        help="More accurate, costs more.",
    ),
    _f(
        "ai.max_images",
        "Max photos per listing",
        group="alerts",
        widget="number",
        help="3 is plenty.",
    ),
    _f(
        "notifiers.*.ntfy.type",
        "Kind of alert",
        group="notifications",
        widget="notifier-type",
        help="",
    ),
    _f(
        "notifiers.*.ntfy.topic",
        "ntfy channel name",
        group="notifications",
        widget="topic",
        help="Anyone who knows it can read your alerts. Subscribe to it in the ntfy app.",
    ),
    _f(
        "notifiers.*.ntfy.server",
        "ntfy server",
        group="notifications",
        advanced=True,
        help="Leave as https://ntfy.sh unless you self-host.",
    ),
    _f(
        "notifiers.*.ntfy.priority",
        "Alert priority",
        group="notifications",
        widget="number",
        advanced=True,
        help="Blank = app default. 5 gets through Do Not Disturb.",
    ),
    _f(
        "notifiers.*.telegram.type",
        "Kind of alert",
        group="notifications",
        widget="notifier-type",
        help="",
    ),
    _f(
        "notifiers.*.telegram.bot_token",
        "Bot token",
        group="notifications",
        widget="secret",
        warning="Telegram alerts aren't built yet - a scan will stop with an error.",
        help="From @BotFather. ${TELEGRAM_BOT_TOKEN} also works.",
    ),
    _f(
        "notifiers.*.telegram.chat_id",
        "Chat ID",
        group="notifications",
        widget="secret",
        help="${TELEGRAM_CHAT_ID} also works.",
    ),
    # --- where -------------------------------------------------------------------
    _f(
        "marketplaces.*.enabled",
        "Enabled",
        group="where",
        widget="toggle",
        help="Off pauses it.",
    ),
    _f(
        "marketplaces.*.default_location",
        "Your city",
        group="where",
        help="As the marketplace writes it, e.g. \"Toronto, ON\".",
    ),
    _f(
        "marketplaces.*.default_radius_km",
        "Radius",
        group="where",
        widget="number",
        unit="km",
        help="40 km covers a metro area.",
    ),
    _f(
        "marketplaces.*.fetch_details",
        "Read full listing pages",
        group="where",
        widget="toggle",
        advanced=True,
        help="Reads the full description. Much more accurate; off is faster and cheaper.",
    ),
    _f(
        "marketplaces.*.session_path",
        "Saved sign-in location",
        group="where",
        advanced=True,
        help="Blank = standard location.",
    ),
    # --- when --------------------------------------------------------------------
    _f(
        "schedule.poll_interval_seconds",
        "Check every",
        group="when",
        widget="select",
        options=_MINUTE_OPTIONS,
        help="30 minutes recommended.",
    ),
    _f(
        "scan.max_evaluations_per_item",
        "Listings to rate per scan",
        group="when",
        widget="number",
        help="Each is one paid AI request. 0 = free, no AI.",
    ),
    _f(
        "scan.max_listings_per_search",
        "Results to read per search",
        group="when",
        widget="number",
        advanced=True,
        help="Higher digs further back, slower.",
    ),
    _f(
        "schedule.jitter_seconds",
        "Random extra wait",
        group="when",
        widget="number",
        unit="seconds",
        advanced=True,
        help="Random extra delay between checks.",
    ),
    _f(
        "schedule.per_request_min_interval_seconds",
        "Pause between page loads",
        group="when",
        widget="number",
        unit="seconds",
        advanced=True,
        help="Lower is faster but riskier.",
    ),
    # --- messaging ----------------------------------------------------------------
    _f(
        "messaging.enabled",
        "Draft messages to sellers",
        group="messaging",
        widget="toggle",
        warning=(
            "Automated messaging may go against a marketplace's terms of service. Use it "
            "sparingly, and read every draft before you approve it."
        ),
        help="Drafts go to Message drafts. Nothing is sent until you approve it.",
    ),
    _f(
        "messaging.negotiate",
        "Open with an offer",
        group="messaging",
        widget="toggle",
        help="Off just asks if it's available.",
    ),
    _f(
        "messaging.offer_percent",
        "Offer % of asking",
        group="messaging",
        widget="percent",
        help="Rounded to $5, never above asking.",
    ),
    # --- advanced -----------------------------------------------------------------
    _f(
        "version",
        "Settings file version",
        group="advanced",
        widget="number",
        advanced=True,
        help="Leave at 1.",
    ),
    _f(
        "ai.provider",
        "AI service",
        group="advanced",
        widget="select",
        options=[{"value": "anthropic", "label": "Anthropic (Claude)"}],
        advanced=True,
        help="Only Claude for now.",
    ),
    _f(
        "ai.model",
        "Claude model",
        group="advanced",
        widget="combo",
        options=_MODEL_OPTIONS,
        advanced=True,
        help="Haiku is cheapest (recommended). Sonnet ~3x, Opus ~5x per listing.",
    ),
    _f(
        "ai.max_tokens",
        "Reply length limit",
        group="advanced",
        widget="number",
        unit="tokens",
        advanced=True,
        help="1024 is plenty.",
    ),
    _f(
        "ai.api_key_env",
        "API key variable name",
        group="advanced",
        advanced=True,
        help="Your key is read from this environment variable, never stored here.",
    ),
)


def bounds_for(info: FieldInfo) -> dict[str, Any]:
    """Read min/max/required straight off the pydantic field.

    Retyping these into the spec by hand is how browser validation drifts away
    from what the server accepts.
    """
    out: dict[str, Any] = {"required": info.is_required()}
    for meta in info.metadata:
        if isinstance(meta, Ge):
            out["min"] = meta.ge
        elif isinstance(meta, Le):
            out["max"] = meta.le
        elif isinstance(meta, MinLen):
            out["min_items"] = meta.min_length
    return out


def schema_field_infos() -> dict[str, FieldInfo]:
    """Every schema field, keyed by the dotted path the form uses."""
    fields: dict[str, FieldInfo] = {}
    for model, prefix in MODEL_PREFIXES:
        for name, info in model.model_fields.items():
            # Nested models are described by their own entry in MODEL_PREFIXES.
            if model is AppConfig and name in NESTED_CONTAINERS:
                continue
            fields[f"{prefix}.{name}" if prefix else name] = info
    return fields


def spec_for(path: str) -> dict[str, Any] | None:
    return next((dict(f) for f in FIELDS if f["path"] == path), None)


def build_formspec() -> dict[str, Any]:
    """The whole spec, with bounds merged in, ready to serve as JSON."""
    infos = schema_field_infos()
    fields = []
    for field in FIELDS:
        merged = dict(field)
        info = infos.get(field["path"])
        if info is not None:
            merged.update(bounds_for(info))
            merged["default"] = None if info.is_required() else _jsonable(info.get_default())
        fields.append(merged)
    return {
        "groups": list(GROUPS),
        "fields": fields,
        "capabilities": {
            "marketplaces": list(IMPLEMENTED_MARKETPLACES),
            "notifiers": list(IMPLEMENTED_NOTIFIERS),
        },
        "example_description": EXAMPLE_DESCRIPTION,
    }


def _jsonable(value: Any) -> Any:
    from pydantic_core import PydanticUndefined

    if value is PydanticUndefined:
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return None
