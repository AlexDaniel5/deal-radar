"""Load, env-resolve, and validate the YAML config."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from .schema import AppConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` in string values from the environment."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                return os.environ[name]
            except KeyError as exc:
                raise ConfigError(
                    f"environment variable {name!r} referenced in config is not set",
                    kind="env",
                ) from exc

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def collect_env_refs(text: str) -> list[str]:
    """Every distinct ``${VAR}`` name referenced anywhere in raw config text.

    Used by the web UI to report *unset* variables as warnings rather than as
    errors: a document shouldn't be un-editable just because a runtime secret
    isn't exported in the shell running the server. :func:`load_config` still
    enforces them strictly at scan time.
    """
    return sorted({m.group(1) for m in _ENV_PATTERN.finditer(text)})


def missing_env_refs(text: str) -> list[str]:
    """The subset of :func:`collect_env_refs` not currently set in the environment."""
    return [name for name in collect_env_refs(text) if name not in os.environ]


def _parse_and_validate(text: str, *, source: str, resolve_env: bool) -> AppConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise ConfigError(
            f"invalid YAML in {source}: {exc}",
            kind="yaml",
            line=None if mark is None else mark.line + 1,
            column=None if mark is None else mark.column + 1,
        ) from exc
    if raw is None:
        raise ConfigError(f"config is empty: {source}", kind="empty")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config root must be a mapping, got {type(raw).__name__}", kind="yaml"
        )
    if resolve_env:
        raw = _resolve_env(raw)
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"config validation failed for {source}:\n{exc}",
            kind="schema",
            errors=[dict(e) for e in exc.errors(include_url=False)],
        ) from exc


def load_config(path: str | Path, *, resolve_env: bool = True) -> AppConfig:
    """Read a YAML config file and return a validated :class:`AppConfig`.

    Raises :class:`ConfigError` on any missing file, malformed YAML, unresolved
    ``${ENV}`` reference, or schema validation failure.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    return _parse_and_validate(
        p.read_text(encoding="utf-8"), source=str(p), resolve_env=resolve_env
    )


def validate_config_text(text: str, *, resolve_env: bool = True) -> AppConfig:
    """Validate raw YAML config text (e.g. from the web editor) without touching disk.

    Raises :class:`ConfigError` on malformed YAML, unresolved ``${ENV}``, or schema
    failure. Returns the validated :class:`AppConfig` on success.
    """
    return _parse_and_validate(text, source="<submitted config>", resolve_env=resolve_env)


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Best-effort load of a local ``.env`` so ``${VAR}`` refs resolve in dev.

    Existing environment variables are not overridden.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)
