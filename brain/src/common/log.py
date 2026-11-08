"""JSON-lines logging: one line per event, with correlation_id when there is one."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from .config import resolve


def iso_utc(t: float) -> str:
    """A Unix time as ISO 8601 UTC with milliseconds, e.g. 2026-11-14T10:22:03.120Z."""
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    """Now as ISO 8601 UTC with milliseconds, e.g. 2026-11-14T10:22:03.120Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"ts": utc_now_iso(), "level": record.levelname, "module": record.name}
        line.update(getattr(record, "fields", {}))
        return json.dumps(line, ensure_ascii=False, default=str)


class EventLogger:
    def __init__(self, module: str, file: str | None = None, level: str = "INFO"):
        self._log = logging.getLogger(module)
        self._log.setLevel(level)
        self._log.propagate = False
        if not self._log.handlers:
            fmt = _JsonFormatter()
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(fmt)
            self._log.addHandler(stream)
            if file:
                path = resolve(file)
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(path, encoding="utf-8")
                fh.setFormatter(fmt)
                self._log.addHandler(fh)

    def event(self, event: str, level: int = logging.INFO, **fields: Any) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(level, event, extra={"fields": {"event": event, **fields}})

    def info(self, event: str, **fields: Any) -> None:
        self.event(event, logging.INFO, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.event(event, logging.WARNING, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.event(event, logging.ERROR, **fields)
