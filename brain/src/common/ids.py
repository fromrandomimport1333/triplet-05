"""Message ids prefixed by module: in-000501, det-000031, ..."""

from __future__ import annotations

import itertools
import threading


class IdGenerator:
    def __init__(self, prefix: str, width: int = 6):
        self._prefix = prefix
        self._width = width
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            n = next(self._counter)
        return f"{self._prefix}-{n:0{self._width}d}"
