"""Prompt construction and the structured verdict schema for AI evaluation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config.schema import ItemConfig
from ..models import Listing

SYSTEM = (
    "You are a careful buying assistant for online marketplace listings. Given a shopper's "
    "description of what they want and a single listing, decide (1) whether the listing is "
    "genuinely the kind of item they want and (2) how good a deal it is. Be strict: reject wrong "
    "categories, parts-only or broken items, accessories sold as the item, and obvious mismatches. "
    "Judge deal quality against typical resale value for that specific item in good condition. "
    "Answer only through the provided structured schema."
)


class Verdict(BaseModel):
    """Structured output returned by Claude for one listing."""

    match: bool = Field(
        description="True only if the listing genuinely matches what the shopper wants."
    )
    rating: int = Field(
        ge=1,
        le=5,
        description="1 = irrelevant or a bad deal; 5 = excellent match and a great price.",
    )
    rationale: str = Field(description="One concise sentence explaining the verdict.")


def build_user_prompt(item: ItemConfig, listing: Listing) -> str:
    """Render the per-listing user message."""
    if listing.price is not None:
        price = f"{listing.price:.0f} {listing.currency}"
    else:
        price = "unknown"
    parts = [
        "## What I'm looking for",
        f"Item: {item.name}",
        item.description.strip(),
        "",
        "## Listing under consideration",
        f"Title: {listing.title}",
        f"Price: {price}",
        f"Location: {listing.location or 'unknown'}",
        "Description:",
        listing.description.strip() or "(none provided)",
    ]
    return "\n".join(parts)
