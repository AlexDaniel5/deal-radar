"""Test-wide safety net: never touch the developer's real data.

deal-radar keeps its "already checked" listings and message drafts in a single
SQLite file under ``~/.local/share/deal-radar`` (or ``$DEAL_RADAR_DATA_DIR``).
Endpoint tests resolve that path at call time, so a test that forgets to
override the location will happily wipe real data through a route like
``/api/seen/clear`` — which is exactly what happened once.

Individual tests may still point ``DEAL_RADAR_DATA_DIR`` somewhere they control;
this only makes *forgetting* to harmless.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REAL_STORE = Path.home() / ".local" / "share" / "deal-radar" / "seen.sqlite3"


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    previous = os.environ.get("DEAL_RADAR_DATA_DIR")
    os.environ["DEAL_RADAR_DATA_DIR"] = str(tmp_path_factory.mktemp("deal-radar-data"))
    before = REAL_STORE.stat().st_mtime_ns if REAL_STORE.is_file() else None
    try:
        yield
    finally:
        after = REAL_STORE.stat().st_mtime_ns if REAL_STORE.is_file() else None
        if previous is None:
            os.environ.pop("DEAL_RADAR_DATA_DIR", None)
        else:
            os.environ["DEAL_RADAR_DATA_DIR"] = previous
        assert before == after, (
            f"this test modified the real data store at {REAL_STORE}; "
            "it must point DEAL_RADAR_DATA_DIR somewhere disposable"
        )
