"""Pipeline step 2 — replay protection.

Two independent defences:
1. Sliding-window dedup cache of event_ids (bounded by size AND age).
2. Per-device monotonic ``seq``: an event whose seq is <= the last accepted seq for that
   device is a replay even if its event_id has already been evicted from the cache.
"""
from __future__ import annotations

from collections import OrderedDict


class ReplayGuard:
    """Detects duplicate event_ids and non-monotonic per-device sequence numbers."""

    def __init__(self, max_entries: int = 100_000, window_s: float = 3600.0) -> None:
        """Create a guard remembering at most `max_entries` ids for at most `window_s` seconds."""
        self.max_entries = max_entries
        self.window_s = window_s
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_seq: dict[str, int] = {}

    def _evict(self, now: float) -> None:
        while self._seen:
            eid, t = next(iter(self._seen.items()))
            if now - t > self.window_s or len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
            else:
                break

    def check(self, event_id: str, device_id: str, seq: int, now: float) -> tuple[bool, str]:
        """Return (ok, reason) WITHOUT recording. ok=False means the event is a replay."""
        self._evict(now)
        if event_id in self._seen:
            return False, f"replay: duplicate event_id {event_id}"
        last = self._last_seq.get(device_id)
        if last is not None and seq <= last:
            return False, f"replay: non-monotonic seq {seq} <= {last} for {device_id}"
        return True, "ok"

    def record(self, event_id: str, device_id: str, seq: int, now: float) -> None:
        """Remember an event that passed the replay check."""
        self._seen[event_id] = now
        self._seen.move_to_end(event_id)
        self._last_seq[device_id] = max(seq, self._last_seq.get(device_id, -1))
        self._evict(now)

    def check_and_record(self, event_id: str, device_id: str, seq: int, now: float) -> tuple[bool, str]:
        """Atomic check + record (records only when the check passes)."""
        ok, why = self.check(event_id, device_id, seq, now)
        if ok:
            self.record(event_id, device_id, seq, now)
        return ok, why

    def __len__(self) -> int:
        return len(self._seen)
