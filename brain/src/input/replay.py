"""Replay protection (SEC-13): ids seen in the last N seconds, and increasing `seq` per device."""

from __future__ import annotations

from typing import Callable


class ReplayCache:
    def __init__(self, window_s: float, cleanup_s: float, clock: Callable[[], float]):
        self.window_s = window_s
        self.cleanup_s = cleanup_s
        self._clock = clock
        self._seen: dict[tuple[str, str], float] = {}
        self._last_cleanup = clock()

    def check_and_add(self, sender: str, message_id: str) -> bool:
        """True if new (and remember it), False if seen within the window."""
        now = self._clock()
        if now - self._last_cleanup >= self.cleanup_s:
            self._cleanup(now)
        key = (sender, message_id)
        first = self._seen.get(key)
        if first is not None and now - first < self.window_s:
            return False
        self._seen[key] = now
        return True

    def add(self, sender: str, message_id: str) -> None:
        self._seen[(sender, message_id)] = self._clock()

    def _cleanup(self, now: float) -> None:
        limit = now - self.window_s
        self._seen = {k: t for k, t in self._seen.items() if t >= limit}
        self._last_cleanup = now

    def __len__(self) -> int:
        return len(self._seen)


class SeqTracker:
    """If a sender adds `seq`, it must strictly increase per device."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}

    def check_and_update(self, sender: str, device: str, seq: int) -> int | None:
        """None if accepted, otherwise the last seq seen."""
        key = (sender, device)
        last = self._last.get(key)
        if last is not None and seq <= last:
            return last
        self._last[key] = seq
        return None

    def reset(self, sender: str) -> None:
        """A layer restarted (status online again): its counters start over."""
        self._last = {k: v for k, v in self._last.items() if k[0] != sender}
