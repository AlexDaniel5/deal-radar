"""Tests for the .env writer behind the wizard's API-key field.

The wizard writes a secret to a file from a web request, so the two things
that matter most are: other lines survive, and the result isn't world-readable.
"""

from __future__ import annotations

import stat
from pathlib import Path

from deal_radar.web.dotenv_io import read_env_var, remove_env_var, upsert_env_var


def test_creates_the_file_when_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert_env_var(env, "ANTHROPIC_API_KEY", "sk-ant-abc")
    assert read_env_var(env, "ANTHROPIC_API_KEY") == "sk-ant-abc"


def test_replaces_in_place_and_keeps_everything_else(tmp_path: Path) -> None:
    """A wizard save must never cost the user their other secrets or comments."""
    env = tmp_path / ".env"
    env.write_text(
        "# my notes\n"
        "OTHER_SECRET=keep-me\n"
        "ANTHROPIC_API_KEY=sk-ant-old\n"
        "\n"
        "TRAILING=also-keep\n"
    )
    upsert_env_var(env, "ANTHROPIC_API_KEY", "sk-ant-new")
    lines = env.read_text().splitlines()
    assert lines[0] == "# my notes"
    assert "OTHER_SECRET=keep-me" in lines
    assert "TRAILING=also-keep" in lines
    assert "ANTHROPIC_API_KEY=sk-ant-new" in lines
    assert "sk-ant-old" not in env.read_text()
    # Position is preserved, so the file doesn't get reshuffled on every save.
    assert lines.index("ANTHROPIC_API_KEY=sk-ant-new") == 2


def test_appends_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")
    upsert_env_var(env, "NEW_KEY", "value")
    assert env.read_text() == "EXISTING=1\nNEW_KEY=value\n"


def test_appends_cleanly_when_the_file_lacks_a_final_newline(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1")  # no trailing newline
    upsert_env_var(env, "NEW_KEY", "value")
    assert env.read_text() == "EXISTING=1\nNEW_KEY=value\n"


def test_handles_export_prefix(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("export ANTHROPIC_API_KEY=old\n")
    upsert_env_var(env, "ANTHROPIC_API_KEY", "new")
    assert "old" not in env.read_text()
    assert read_env_var(env, "ANTHROPIC_API_KEY") == "new"


def test_quotes_values_that_need_it(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert_env_var(env, "WEIRD", 'has space and "quote"')
    assert read_env_var(env, "WEIRD") == 'has space and "quote"'


def test_file_is_owner_only(tmp_path: Path) -> None:
    """A secret written by a web request must not be world-readable."""
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")
    env.chmod(0o644)
    upsert_env_var(env, "ANTHROPIC_API_KEY", "sk-ant-abc")
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert_env_var(env, "A", "1")
    upsert_env_var(env, "B", "2")
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_remove(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP=1\nDROP=2\n")
    assert remove_env_var(env, "DROP") is True
    assert env.read_text() == "KEEP=1\n"
    assert remove_env_var(env, "DROP") is False  # already gone


def test_read_missing_file_and_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert read_env_var(env, "NOPE") is None
    env.write_text("A=1\n")
    assert read_env_var(env, "NOPE") is None
