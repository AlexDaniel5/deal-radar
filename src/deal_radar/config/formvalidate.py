"""Field-scoped errors for the settings form.

Pydantic's model-level validators report against the *model*, not the field:
``_check_prices`` fails at ``("items", 0)`` with no leaf, so a form has nothing
to highlight. These checks run first and produce the same failures with a real
field location and copy written for someone who isn't reading a stack trace.

Pydantic still runs afterwards and remains the authority on what's valid; this
only makes two of its errors pointable-at.
"""

from __future__ import annotations

from typing import Any

from ..marketplaces.registry import IMPLEMENTED_MARKETPLACES
from ..notifiers.registry import IMPLEMENTED_NOTIFIERS

__all__ = ["capability_warnings", "cross_field_errors", "friendly_message"]


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def cross_field_errors(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Errors pydantic would report without a field to attach them to."""
    errors: list[dict[str, Any]] = []
    items = raw.get("items")
    known = set(raw.get("marketplaces") or {})

    if isinstance(items, list):
        seen_names: dict[str, int] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            low, high = item.get("price_min"), item.get("price_max")
            if isinstance(low, int | float) and isinstance(high, int | float) and low > high:
                errors.append(
                    {
                        "loc": ["items", index, "price_max"],
                        "type": "price_order",
                        "msg": (
                            f"The highest price ({_money(high)}) is below the lowest "
                            f"price ({_money(low)})."
                        ),
                    }
                )
            for name in item.get("marketplaces") or []:
                if name not in known:
                    options = ", ".join(sorted(known)) or "none yet"
                    errors.append(
                        {
                            "loc": ["items", index, "marketplaces"],
                            "type": "unknown_marketplace",
                            "msg": (
                                f"This searches '{name}', which isn't set up. "
                                f"Available: {options}."
                            ),
                        }
                    )
            # Duplicate names silently merge two hunts' history in the seen store.
            name = item.get("name")
            if isinstance(name, str) and name:
                if name in seen_names:
                    errors.append(
                        {
                            "loc": ["items", index, "name"],
                            "type": "duplicate_name",
                            "msg": (
                                f"You already have something called '{name}'. Give this one "
                                "a different name — deal-radar tracks what it has shown you "
                                "by name."
                            ),
                        }
                    )
                seen_names[name] = index
    return errors


def capability_warnings(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Settings that validate but would fail at scan time.

    ``telegram`` and any marketplace other than Facebook pass the schema and
    then raise ``NotImplementedError`` mid-scan. Better to say so while editing.
    """
    warnings: list[dict[str, Any]] = []
    for index, notifier in enumerate(raw.get("notifiers") or []):
        if isinstance(notifier, dict) and notifier.get("type") not in IMPLEMENTED_NOTIFIERS:
            warnings.append(
                {
                    "loc": ["notifiers", index, "type"],
                    "kind": "unsupported",
                    "message": (
                        f"'{notifier.get('type')}' alerts aren't built yet. deal-radar will "
                        "save this, but a scan will stop with an error. Use ntfy for now."
                    ),
                }
            )
    for name in sorted(set(raw.get("marketplaces") or {}) - set(IMPLEMENTED_MARKETPLACES)):
        warnings.append(
            {
                "loc": ["marketplaces", name],
                "kind": "unsupported",
                "message": (
                    f"deal-radar can't search '{name}' yet. A scan will stop with an error "
                    "while it's switched on."
                ),
            }
        )
    return warnings


# Pydantic error types mapped to something a non-technical reader can act on.
# Anything unmapped falls through to pydantic's own message rather than being
# dropped — a confusing error beats a missing one.
_FRIENDLY: dict[str, str] = {
    "missing": "{label} is required.",
    "string_too_short": "{label} can't be empty.",
    "too_short": "Add at least one entry to {label}.",
    "greater_than_equal": "{label} must be {limit} or more.",
    "less_than_equal": "{label} must be {limit} or less.",
    "int_parsing": "{label} must be a whole number.",
    "float_parsing": "{label} must be a number.",
    "bool_parsing": "{label} must be yes or no.",
    "extra_forbidden": (
        "There's no setting called '{last}'. Remove it under Advanced, or check the spelling."
    ),
}


def friendly_message(error: dict[str, Any], label: str | None = None) -> str:
    """Rewrite one pydantic error for a non-technical reader."""
    template = _FRIENDLY.get(str(error.get("type")))
    loc = error.get("loc") or ()
    last = str(loc[-1]) if loc else ""
    if template is None:
        return str(error.get("msg") or "This value isn't valid.")
    ctx = error.get("ctx") or {}
    limit = ctx.get("ge", ctx.get("le", ctx.get("min_length", "")))
    return template.format(label=label or last or "This", last=last, limit=limit)
