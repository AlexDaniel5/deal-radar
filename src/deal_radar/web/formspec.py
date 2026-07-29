"""What the settings form looks like, and what every field is called in English.

The spec lives in Python rather than in the JavaScript so that all this copy is
unit-testable — in particular, a test walks the pydantic models and asserts that
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
        "title": "What I'm hunting for",
        "blurb": (
            "The things deal-radar looks for, and how it decides one is "
            "worth telling you about."
        ),
    },
    {
        "id": "alerts",
        "title": "How you get alerted",
        "blurb": "Where matches are sent, and how good a match has to be.",
    },
    {
        "id": "where",
        "title": "Where to look",
        "blurb": "Which marketplace, which city, how far out.",
    },
    {
        "id": "when",
        "title": "How often to check",
        "blurb": "How frequently deal-radar looks, and how much each look may cost.",
    },
    {
        "id": "messaging",
        "title": "Messaging sellers",
        "blurb": "Off by default. Nothing is ever sent without you reading it first.",
    },
    {
        "id": "advanced",
        "title": "Advanced",
        "blurb": "Rarely needed. If you're not sure, leave these alone.",
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
    ):
        if value is not None:
            spec[key] = value
    return spec


_MODEL_OPTIONS = [
    {"value": c.id, "label": f"{c.label} — {c.note}"} for c in SUPPORTED_MODELS
]

_MINUTE_OPTIONS = [
    {"value": 600, "label": "every 10 minutes"},
    {"value": 900, "label": "every 15 minutes"},
    {"value": 1800, "label": "every 30 minutes (recommended)"},
    {"value": 3600, "label": "every hour"},
    {"value": 7200, "label": "every 2 hours"},
    {"value": 14400, "label": "every 4 hours"},
]

FIELDS: tuple[dict[str, Any], ...] = (
    # --- items -----------------------------------------------------------------
    _f(
        "items.*.name",
        "What are you calling this?",
        group="items",
        help=(
            "Just for you — it appears on your alerts and in the lists below. "
            'Something like "Laptop for school" or "Road bike, 54-56cm".'
        ),
        placeholder="Road bike",
    ),
    _f(
        "items.*.enabled",
        "Actively hunting",
        group="items",
        widget="toggle",
        help="Turn this off to pause it without losing the setup.",
    ),
    _f(
        "items.*.description",
        "Describe exactly what you want — this is what the AI reads",
        group="items",
        widget="textarea",
        rows=10,
        example=EXAMPLE_DESCRIPTION,
        help=(
            "This is the most important box on the page. Write it like instructions to a "
            "knowledgeable friend shopping for you. The AI reads it against every listing "
            "it finds and follows it literally, so anything vague it has to guess at. "
            "Cover four things: what you must have, what would be nice, what's a "
            "deal-breaker, and what counts as a good price."
        ),
    ),
    _f(
        "items.*.search_phrases",
        "Search terms to type into the marketplace",
        group="items",
        widget="chips",
        help=(
            "Each one runs as its own search, exactly as if you typed it into the "
            'marketplace\'s search box. Three to six broad terms works well ("gaming pc", '
            '"rtx 3080"). More terms means a wider net, and a slower, slightly dearer check.'
        ),
        placeholder="road bike",
    ),
    _f(
        "items.*.include_keywords",
        "Related words (helps deal-radar ignore junk results)",
        group="items",
        widget="chips",
        help=(
            "This is NOT a requirements list. Marketplaces pad search results with "
            "unrelated things once the real matches run out — BBQs, apartments. A result "
            "is only considered if its title shares a word with your search terms or this "
            'list. Keep it broad: brand and model names like "thinkpad", "ryzen", "4070", '
            "so a listing that names only a model still gets through. If a genuine listing "
            "ever gets skipped, add a word from its title here."
        ),
    ),
    _f(
        "items.*.exclude_keywords",
        "Never show me listings containing these words",
        group="items",
        widget="chips",
        help=(
            "This is the only hard filter. If any of these words appears anywhere in a "
            'listing\'s title or description it is dropped and never rated. Keep it short '
            'and obvious — "for parts", "broken". A common word here can quietly hide good '
            "listings."
        ),
    ),
    _f(
        "items.*.price_min",
        "Lowest price",
        group="items",
        widget="money",
        prefix="$",
        help="Leave blank for no lower limit.",
    ),
    _f(
        "items.*.price_max",
        "Highest price",
        group="items",
        widget="money",
        prefix="$",
        help=(
            "Leave blank for no upper limit. Listings with no price shown are always "
            "passed to the AI to judge."
        ),
    ),
    _f(
        "items.*.location",
        "City to search",
        group="items",
        help=(
            "Leave blank to use your default city. Type it the way the marketplace "
            'writes it, e.g. "Mississauga, ON".'
        ),
    ),
    _f(
        "items.*.radius_km",
        "How far from that city",
        group="items",
        widget="number",
        unit="km",
        help="Leave blank to use your default distance.",
    ),
    _f(
        "items.*.marketplaces",
        "Search on",
        group="items",
        widget="marketplace-checkboxes",
        help="Only places you've set up appear here.",
    ),
    _f(
        "items.*.min_rating",
        "Only alert me on this one when it scores at least…",
        group="items",
        widget="rating",
        overridable="ai.min_rating",
        help=(
            "Overrides your global setting for this one thing. Set 5 for something "
            "you'd only buy at a steal."
        ),
    ),
    _f(
        "items.*.negotiate",
        "Open with an offer for this one",
        group="items",
        widget="toggle",
        overridable="messaging.negotiate",
        help="Overrides your global messaging setting for this one thing.",
    ),
    _f(
        "items.*.offer_percent",
        "Open at this much of the asking price, for this one",
        group="items",
        widget="percent",
        overridable="messaging.offer_percent",
        help="Overrides your global messaging setting for this one thing.",
    ),
    # --- alerts ------------------------------------------------------------------
    _f(
        "ai.min_rating",
        "Only text me when a find scores at least…",
        group="alerts",
        widget="rating",
        captions=[
            "1 — anything, very noisy",
            "2 — loosely related",
            "3 — a decent match",
            "4 — a strong match at a good price (recommended)",
            "5 — only exceptional finds",
        ],
        help=(
            "deal-radar scores every listing out of 5. Pick 5 to hear only about "
            "exceptional finds; pick 3 to see more, including near-misses."
        ),
    ),
    _f(
        "notify_top_n",
        "How many alerts per check",
        group="alerts",
        widget="number",
        help=(
            "Each time deal-radar checks it finds every match, ranks them best-first, and "
            "texts you only the top few as one message. 5 is a good default."
        ),
    ),
    _f(
        "ai.analyze_images",
        "Let the AI look at listing photos",
        group="alerts",
        widget="toggle",
        help=(
            "Photos are only sent when the seller's text points at them (e.g. "
            '"everything is in the photos"). It makes judgements more accurate, and it '
            "makes each check cost noticeably more."
        ),
    ),
    _f(
        "ai.max_images",
        "Photos per listing (at most)",
        group="alerts",
        widget="number",
        help="More photos means a better judgement and a higher cost. 3 is plenty.",
    ),
    _f(
        "notifiers.*.ntfy.type",
        "Kind of alert",
        group="alerts",
        widget="notifier-type",
        help="",
    ),
    _f(
        "notifiers.*.ntfy.topic",
        "Your private ntfy channel name",
        group="alerts",
        widget="topic",
        help=(
            "Like a secret channel name — anyone who knows it can read your alerts, so "
            "make it long and random. Install the free ntfy app on your phone, tap "
            "Subscribe, and type this exact name."
        ),
    ),
    _f(
        "notifiers.*.ntfy.server",
        "ntfy server",
        group="alerts",
        advanced=True,
        help="Leave this as https://ntfy.sh unless you run your own ntfy server.",
    ),
    _f(
        "notifiers.*.ntfy.priority",
        "Alert priority",
        group="alerts",
        widget="number",
        advanced=True,
        help=(
            "Blank means whatever the app decides. 5 gets through Do Not Disturb on "
            "most phones."
        ),
    ),
    _f(
        "notifiers.*.telegram.type",
        "Kind of alert",
        group="alerts",
        widget="notifier-type",
        help="",
    ),
    _f(
        "notifiers.*.telegram.bot_token",
        "Bot token",
        group="alerts",
        widget="secret",
        warning="Telegram alerts aren't built yet — a scan will stop with an error.",
        help=(
            "From @BotFather in Telegram. You can type ${TELEGRAM_BOT_TOKEN} here instead "
            "to keep the secret out of this file."
        ),
    ),
    _f(
        "notifiers.*.telegram.chat_id",
        "Chat ID",
        group="alerts",
        widget="secret",
        help="The Telegram chat to send to. ${TELEGRAM_CHAT_ID} also works.",
    ),
    # --- where -------------------------------------------------------------------
    _f(
        "marketplaces.*.enabled",
        "Search this marketplace",
        group="where",
        widget="toggle",
        help="Turn off to pause it without deleting your setup.",
    ),
    _f(
        "marketplaces.*.default_location",
        "Your city",
        group="where",
        help=(
            "Used by anything that doesn't set its own. Type it the way the marketplace "
            'writes it, e.g. "Toronto, ON".'
        ),
    ),
    _f(
        "marketplaces.*.default_radius_km",
        "How far to look",
        group="where",
        widget="number",
        unit="km",
        help="40 km covers a metro area; 100 km reaches the surrounding towns.",
    ),
    _f(
        "marketplaces.*.fetch_details",
        "Open each listing's full page before judging it",
        group="where",
        widget="toggle",
        advanced=True,
        help=(
            "On (recommended): deal-radar reads the seller's full description, which makes "
            "scores much more accurate. Off: it judges from the short blurb on the results "
            "page — faster and cheaper, and much less accurate."
        ),
    ),
    _f(
        "marketplaces.*.session_path",
        "Where the saved sign-in is kept",
        group="where",
        advanced=True,
        help=(
            "Leave blank for the standard location. Only change this if you keep several "
            "separate logins."
        ),
    ),
    # --- when --------------------------------------------------------------------
    _f(
        "schedule.poll_interval_seconds",
        "Check for new listings",
        group="when",
        widget="select",
        options=_MINUTE_OPTIONS,
        help=(
            "Every 30 minutes is a good balance. Checking more often won't find much more "
            "— people don't post that fast — and puts more load on the marketplace. The "
            "minimum allowed is 5 minutes."
        ),
    ),
    _f(
        "scan.max_evaluations_per_item",
        "How many listings to check each time",
        group="when",
        widget="number",
        help=(
            "Each check is one paid AI request, so this is your cost cap. The first scan "
            "costs the most; after that, listings already checked are skipped. Set 0 to "
            "look without asking the AI anything, for free."
        ),
    ),
    _f(
        "scan.max_listings_per_search",
        "How far to scroll the search results",
        group="when",
        widget="number",
        advanced=True,
        help=(
            "How many results to read before stopping. Higher digs further back through "
            "older listings, and takes longer."
        ),
    ),
    _f(
        "schedule.jitter_seconds",
        "Random wobble added to the wait",
        group="when",
        widget="number",
        unit="seconds",
        advanced=True,
        help=(
            "Adds up to this much extra random delay between checks so the timing doesn't "
            "look robotic. 600 is fine."
        ),
    ),
    _f(
        "schedule.per_request_min_interval_seconds",
        "Minimum pause between page loads",
        group="when",
        widget="number",
        unit="seconds",
        advanced=True,
        help=(
            "Slows deal-radar to a human browsing pace. Lowering it makes scans faster and "
            "raises the risk of being blocked."
        ),
    ),
    # --- messaging ----------------------------------------------------------------
    _f(
        "messaging.enabled",
        "Let deal-radar write first messages to sellers",
        group="messaging",
        widget="toggle",
        warning=(
            "Automated messaging may go against a marketplace's terms of service. Use it "
            "sparingly, and read every draft before you approve it."
        ),
        help=(
            "When something matches, deal-radar writes a message for you and puts it in "
            "Message drafts. Nothing is ever sent until you read it and press Approve."
        ),
    ),
    _f(
        "messaging.negotiate",
        "Open with an offer instead of the asking price",
        group="messaging",
        widget="toggle",
        help=(
            "Off: the draft just asks whether the item is still available. On: it politely "
            "names a price."
        ),
    ),
    _f(
        "messaging.offer_percent",
        "Open at this much of the asking price",
        group="messaging",
        widget="percent",
        help=(
            "90% of a $500 listing opens at $450. Rounded to the nearest $5, and never "
            "above the asking price."
        ),
    ),
    # --- advanced -----------------------------------------------------------------
    _f(
        "version",
        "Settings file version",
        group="advanced",
        widget="number",
        advanced=True,
        help="Leave this at 1. It tells deal-radar how to read this file.",
    ),
    _f(
        "ai.provider",
        "AI service",
        group="advanced",
        widget="select",
        options=[{"value": "anthropic", "label": "Anthropic (Claude)"}],
        advanced=True,
        help="Claude is the only service supported right now.",
    ),
    _f(
        "ai.model",
        "Which Claude model rates your listings",
        group="advanced",
        widget="combo",
        options=_MODEL_OPTIONS,
        advanced=True,
        help=(
            "Haiku is the cheapest and fastest, and is what we recommend. Sonnet is "
            "smarter and costs about three times as much per listing. Opus is the most "
            "capable and about five times as much. If you're not sure, leave it alone."
        ),
    ),
    _f(
        "ai.max_tokens",
        "Reply length limit",
        group="advanced",
        widget="number",
        unit="tokens",
        advanced=True,
        help="How much room the AI gets for its answer. 1024 is plenty.",
    ),
    _f(
        "ai.api_key_env",
        "Name of the environment variable holding your Claude API key",
        group="advanced",
        advanced=True,
        help=(
            "Your key is never stored in this file — deal-radar reads it from your "
            "computer's environment under this name. Leave it as ANTHROPIC_API_KEY unless "
            "you know you named it something else."
        ),
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
