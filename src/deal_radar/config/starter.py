"""The minimum viable config, for someone starting from nothing.

Used by both the settings form's empty state and the setup wizard, so the two
can't drift. Deliberately writes only what was asked for: everything else is
left out so schema defaults apply and the file stays short enough to read.
"""

from __future__ import annotations

import secrets

__all__ = ["EXAMPLE_DESCRIPTION", "random_topic", "starter_config_text"]

# A private ntfy channel name. Anyone who knows it can read your alerts, so it
# needs to be unguessable rather than memorable.
_TOPIC_PREFIX = "deal-radar-"


def random_topic() -> str:
    return _TOPIC_PREFIX + secrets.token_hex(5)


# Shown in the form as "what a good description looks like". Concrete about
# must-haves, deal-breakers, and what makes the price good — the three things
# people leave out.
EXAMPLE_DESCRIPTION = """\
A road bike for commuting, frame size 54-56cm.

Must have: working gears and brakes, no rust on the frame, wheels true.
Nice to have: recent service, spare tubes, a rack or mudguards.
Don't want: anything sold "for parts", frames with cracks or dents, bikes
missing wheels, or e-bikes.

A good price is meaningfully below what the same model usually sells for
locally — around $300 for a decent used aluminium frame, less if it needs
tyres or a service."""


def starter_config_text(
    *,
    topic: str,
    location: str | None,
    radius_km: int | None,
    item_name: str,
    search_phrases: list[str],
    description: str,
    price_max: float | None = None,
) -> str:
    """Build a small, commented config from the wizard's answers."""
    lines = [
        "# Created by the deal-radar setup screen.",
        "# You can keep editing it here, or open it in a text editor — both work.",
        "version: 1",
        "",
        "ai:",
        "  min_rating: 4          # only alert me on strong matches (1-5)",
        "",
        "scan:",
        "  max_evaluations_per_item: 25   # cost cap: AI checks per scan, per item",
        "",
        "marketplaces:",
        "  facebook:",
        "    enabled: true",
    ]
    if location:
        lines.append(f'    default_location: "{location}"')
    if radius_km:
        lines.append(f"    default_radius_km: {radius_km}")
    lines += [
        "",
        "notifiers:",
        "  - type: ntfy",
        f"    topic: {topic}   # your private channel — keep this secret",
        "",
        "items:",
        f'  - name: "{item_name}"',
        "    marketplaces: [facebook]",
        "    search_phrases: [" + ", ".join(f'"{p}"' for p in search_phrases) + "]",
    ]
    if price_max is not None:
        lines.append(f"    price_max: {price_max:g}")
    lines += ["    description: >"]
    for paragraph_line in description.strip().splitlines():
        lines.append(f"      {paragraph_line}" if paragraph_line.strip() else "")
    return "\n".join(lines) + "\n"
