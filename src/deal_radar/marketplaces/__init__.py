"""Marketplace adapters (pluggable search/parse behind a common interface)."""

from __future__ import annotations

from .base import Marketplace, SearchContext

__all__ = ["Marketplace", "SearchContext"]
