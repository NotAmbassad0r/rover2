"""In-process log ring buffer for GET /api/logs."""

from __future__ import annotations

import collections
import datetime
import logging
import threading

_LOG_BUFFER_MAX = 200


class RingLogHandler(logging.Handler):
    """Thread-safe fixed-size ring buffer on the root logger."""

    def __init__(self, maxlen: int = _LOG_BUFFER_MAX) -> None:
        super().__init__()
        self._buf: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "timestamp": datetime.datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with self._lock:
            self._buf.append(entry)

    def get_records(self) -> list[dict]:
        with self._lock:
            return list(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_handler = RingLogHandler()


def install_log_ring_buffer() -> RingLogHandler:
    """Attach ring handler to root logger (idempotent)."""
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def get_log_handler() -> RingLogHandler:
    return _handler
