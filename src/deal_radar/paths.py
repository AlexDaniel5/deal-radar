"""Filesystem locations for runtime state (seen store, browser sessions)."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "deal-radar"


def data_dir() -> Path:
    """Directory for persistent runtime state.

    Override with ``DEAL_RADAR_DATA_DIR``; otherwise uses ``XDG_DATA_HOME`` or
    ``~/.local/share/deal-radar``.
    """
    override = os.environ.get("DEAL_RADAR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def db_path() -> Path:
    """Path to the SQLite 'seen' store."""
    return data_dir() / "seen.sqlite3"


def default_session_path(marketplace: str) -> Path:
    """Default path for a marketplace's persisted logged-in browser session."""
    return data_dir() / "sessions" / f"{marketplace}.json"


def ensure_data_dir() -> Path:
    """Create the data directory if needed and return it."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
