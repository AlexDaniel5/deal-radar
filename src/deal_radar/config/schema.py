"""Pydantic models describing the deal-radar config file.

Secrets are never stored here directly; string fields may contain ``${ENV_VAR}``
references that the loader resolves from the environment.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AIConfig(BaseModel):
    """How listings are evaluated by Claude."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-haiku-4-5"
    min_rating: int = Field(4, ge=1, le=5, description="Global default notify threshold (1-5).")
    analyze_images: bool = False
    max_images: int = Field(3, ge=0)
    max_tokens: int = Field(1024, ge=256)
    api_key_env: str = "ANTHROPIC_API_KEY"


class MarketplaceConfig(BaseModel):
    """Per-marketplace settings (e.g. the 'facebook' entry)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    session_path: str | None = None
    default_location: str | None = None
    default_radius_km: int | None = Field(None, ge=1)
    fetch_details: bool = Field(
        True,
        description="Open each candidate's detail page for full text before AI evaluation.",
    )


class MessagingConfig(BaseModel):
    """Drafting messages to sellers on matched listings (OFF by default; ToS-sensitive).

    When enabled, a match creates a *draft* message that must be approved in the
    web UI before anything is sent — nothing ever goes out automatically.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    negotiate: bool = False
    offer_percent: int = Field(
        90,
        ge=50,
        le=100,
        description="Opening offer as % of asking, rounded to the nearest $5, never above asking.",
    )


class ScheduleConfig(BaseModel):
    """Polling cadence and politeness controls."""

    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int = Field(1800, ge=300, description="Min 300s for politeness.")
    jitter_seconds: int = Field(600, ge=0)
    per_request_min_interval_seconds: int = Field(25, ge=0)


class NtfyNotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ntfy"] = "ntfy"
    topic: str
    server: str = "https://ntfy.sh"
    priority: int | None = Field(None, ge=1, le=5)


class TelegramNotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["telegram"] = "telegram"
    bot_token: str
    chat_id: str


NotifierConfig = Annotated[
    NtfyNotifierConfig | TelegramNotifierConfig,
    Field(discriminator="type"),
]


class ItemConfig(BaseModel):
    """One thing you're hunting for."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    marketplaces: list[str] = Field(min_length=1)
    search_phrases: list[str] = Field(min_length=1)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    price_min: float | None = Field(None, ge=0)
    price_max: float | None = Field(None, ge=0)
    location: str | None = None
    radius_km: int | None = Field(None, ge=1)
    description: str = Field(min_length=1, description="Free text the AI judges listings against.")
    min_rating: int | None = Field(
        None, ge=1, le=5, description="Overrides ai.min_rating for this item."
    )
    negotiate: bool | None = Field(None, description="Overrides messaging.negotiate for this item.")
    offer_percent: int | None = Field(
        None, ge=50, le=100, description="Overrides messaging.offer_percent for this item."
    )

    @model_validator(mode="after")
    def _check_prices(self) -> ItemConfig:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError(
                f"item {self.name!r}: price_min ({self.price_min}) > price_max ({self.price_max})"
            )
        return self

    def effective_min_rating(self, ai: AIConfig) -> int:
        return self.min_rating if self.min_rating is not None else ai.min_rating

    def effective_negotiate(self, messaging: MessagingConfig) -> bool:
        return self.negotiate if self.negotiate is not None else messaging.negotiate

    def effective_offer_percent(self, messaging: MessagingConfig) -> int:
        return self.offer_percent if self.offer_percent is not None else messaging.offer_percent


class AppConfig(BaseModel):
    """Top-level config."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    ai: AIConfig = Field(default_factory=AIConfig)
    notify_top_n: int = Field(
        5,
        ge=1,
        description="Per scan, notify only the best N matches per item as one ranked digest.",
    )
    marketplaces: dict[str, MarketplaceConfig] = Field(default_factory=dict)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    notifiers: list[NotifierConfig] = Field(min_length=1)
    items: list[ItemConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_marketplace_refs(self) -> AppConfig:
        known = set(self.marketplaces)
        for item in self.items:
            for mp in item.marketplaces:
                if mp not in known:
                    raise ValueError(
                        f"item {item.name!r} references unknown marketplace {mp!r}; "
                        f"configured marketplaces: {sorted(known) or '(none)'}"
                    )
        return self
