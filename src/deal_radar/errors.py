"""Exception taxonomy for deal-radar."""

from __future__ import annotations


class DealRadarError(Exception):
    """Base class for all deal-radar errors."""


class ConfigError(DealRadarError):
    """Raised when configuration is missing, malformed, or invalid."""


class SearchError(DealRadarError):
    """Raised when a marketplace search or parse fails."""


class EvalError(DealRadarError):
    """Raised when AI evaluation fails."""


class NotifyError(DealRadarError):
    """Raised when delivering a notification fails."""
