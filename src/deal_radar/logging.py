"""Structured logging setup."""

from __future__ import annotations

import itertools
import json
import logging
import sys
import threading
from collections import deque

_ROOT = "deal_radar"

_LINE_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    """Configure the ``deal_radar`` logger hierarchy. Safe to call repeatedly."""
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    root = logging.getLogger(_ROOT)
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. ``get_logger("cli")``."""
    return logging.getLogger(f"{_ROOT}.{name}")


class LogBuffer:
    """A bounded, thread-safe ring buffer of formatted log lines.

    Each line gets a monotonically increasing sequence number so a reader (e.g.
    the web UI's SSE stream) can poll for "lines since N" without duplicates.
    """

    def __init__(self, capacity: int = 500) -> None:
        self._lines: deque[tuple[int, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append((next(self._seq), line))

    def since(self, after_seq: int) -> list[tuple[int, str]]:
        with self._lock:
            return [pair for pair in self._lines if pair[0] > after_seq]

    def recent(self, limit: int = 200) -> list[tuple[int, str]]:
        with self._lock:
            items = list(self._lines)
        return items[-limit:]


class _BufferHandler(logging.Handler):
    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer
        self.setFormatter(_LINE_FORMAT)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            pass


def attach_log_buffer(capacity: int = 500) -> LogBuffer:
    """Add an in-memory buffer handler to the deal_radar logger; return the buffer."""
    buffer = LogBuffer(capacity)
    logging.getLogger(_ROOT).addHandler(_BufferHandler(buffer))
    return buffer
