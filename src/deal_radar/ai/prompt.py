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


COMPOSE_SYSTEM = (
    "You write short, polite, casual first messages from a buyer to a marketplace seller. "
    "Sound like a real person: 1-3 short sentences, no emojis, no haggling pressure, no "
    "questions beyond availability and pickup. If an offer amount is provided, include it "
    "exactly once, phrased politely (e.g. 'would you take $X?'). Never invent facts about "
    "the item and never mention AI. Answer only through the provided structured schema."
)


class DraftMessage(BaseModel):
    """Structured output: the message to send the seller."""

    message: str = Field(description="The exact message text to send, 1-3 short sentences.")


def build_compose_prompt(item: ItemConfig, listing: Listing, offer_price: int | None) -> str:
    """Render the user message for composing a first message to the seller."""
    if listing.price is not None:
        price = f"{listing.price:.0f} {listing.currency}"
    else:
        price = "unknown"
    if offer_price is not None:
        instruction = f"Include an opening offer of {offer_price} {listing.currency}."
    else:
        instruction = "Ask if it's still available; you intend to pay the asking price."
    parts = [
        "## Listing I want to buy",
        f"Title: {listing.title}",
        f"Asking price: {price}",
        f"Location: {listing.location or 'unknown'}",
        "Description (excerpt):",
        listing.description.strip()[:300] or "(none provided)",
        "",
        "## Your task",
        f"Write my first message to the seller. {instruction}",
    ]
    return "\n".join(parts)


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
