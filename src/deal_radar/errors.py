"""Exception taxonomy for deal-radar."""

from __future__ import annotations

from typing import Any


class DealRadarError(Exception):
    """Base class for all deal-radar errors."""


class ConfigError(DealRadarError):
    """Raised when configuration is missing, malformed, or invalid.

    Besides the human-readable message, this carries the machine-readable
    detail the web UI needs to point at the specific field that is wrong:

    ``kind``
        ``"schema"`` (pydantic rejected it), ``"yaml"`` (it didn't parse),
        ``"env"`` (an unset ``${VAR}``), or ``"other"``.
    ``errors``
        For ``kind="schema"``, ``ValidationError.errors()`` — each entry has a
        ``loc`` tuple the form maps back to a control.
    ``line`` / ``column``
        For ``kind="yaml"``, 1-based position of the syntax error.

    ``str(exc)`` is unchanged, so the CLI keeps printing exactly what it did.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "other",
        errors: list[dict[str, Any]] | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.errors = errors or []
        self.line = line
        self.column = column


class SearchError(DealRadarError):
    """Raised when a marketplace search or parse fails."""


class EvalError(DealRadarError):
    """Raised when AI evaluation fails."""


class NotifyError(DealRadarError):
    """Raised when delivering a notification fails."""


class SendError(DealRadarError):
    """Raised when sending a marketplace message to a seller fails."""
