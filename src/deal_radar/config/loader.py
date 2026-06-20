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
                    f"environment variable {name!r} referenced in config is not set"
                ) from exc

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_config(path: str | Path, *, resolve_env: bool = True) -> AppConfig:
    """Read a YAML config file and return a validated :class:`AppConfig`.

    Raises :class:`ConfigError` on any missing file, malformed YAML, unresolved
    ``${ENV}`` reference, or schema validation failure.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"config file is empty: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    if resolve_env:
        raw = _resolve_env(raw)
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config validation failed for {p}:\n{exc}") from exc


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Best-effort load of a local ``.env`` so ``${VAR}`` refs resolve in dev.

    Existing environment variables are not overridden.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)
