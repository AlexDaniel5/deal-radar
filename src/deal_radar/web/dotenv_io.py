"""Read and write single variables in a ``.env`` file, safely.

The setup wizard lets someone paste an API key in the browser rather than
editing a dotfile in a terminal. That means writing to ``.env`` from a request
handler, so the write has to be atomic (never leave a half-written file that
would lose *other* secrets) and the result has to be private (0600).
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

# KEY=value, tolerating leading whitespace and an "export " prefix.
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR  # 0600


def _quote(value: str) -> str:
    """Quote only when needed, so simple keys stay readable in the file."""
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_private(path: Path, lines: list[str]) -> None:
    """Atomically replace ``path`` with ``lines``, owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        with tmp:
            tmp.write("".join(lines))
        os.chmod(tmp.name, _OWNER_ONLY)  # set before it takes the real name
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def upsert_env_var(path: str | Path, name: str, value: str) -> Path:
    """Set ``name`` in the ``.env`` at ``path``, preserving everything else.

    Replaces the variable in place if it is already there — keeping its
    position, and keeping every other line and comment untouched — otherwise
    appends it.
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True) if p.is_file() else []
    new_line = f"{name}={_quote(value)}\n"

    for i, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    _write_private(p, lines)
    return p


def remove_env_var(path: str | Path, name: str) -> bool:
    """Delete ``name`` from the ``.env``. Returns True if anything was removed."""
    p = Path(path)
    if not p.is_file():
        return False
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [
        line
        for line in lines
        if not ((m := _ASSIGNMENT.match(line)) and m.group(1) == name)
    ]
    if len(kept) == len(lines):
        return False
    _write_private(p, kept)
    return True


def read_env_var(path: str | Path, name: str) -> str | None:
    """Read a raw value straight from the file (not the process environment)."""
    p = Path(path)
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            raw = line.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return raw
    return None
