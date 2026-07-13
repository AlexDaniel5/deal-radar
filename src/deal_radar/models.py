"""Core domain types passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Listing:
    """A single marketplace listing after parsing."""

    id: str
    marketplace: str
    title: str
    url: str
    price: float | None = None
    currency: str = "USD"
    location: str | None = None
    description: str = ""
    image_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Evaluation:
    """The AI's verdict on a listing for a given item."""

    match: bool
    rating: int  # 1-5
    rationale: str
    model: str
    images_analyzed: bool = False  # photos were attached to the AI call


@dataclass(slots=True)
class NotificationEvent:
    """A match worth telling the operator about."""

    item_name: str
    listing: Listing
    evaluation: Evaluation
    draft_pending: bool = False  # a seller-message draft awaits approval in the web UI
