"""Guardian's contribution to the triplet's shared timeline screen (R3).

UI-framework agnostic: entries are plain JSON, exposed via
  * GET /timeline?since=<iso>&limit=<n>      (pull, JWT tool='timeline')
  * MQTT <TOPIC_TIMELINE> (default triplet/timeline/guardian)  (push)
"""
from __future__ import annotations

import itertools
import threading
from collections import deque

from .events import iso, utcnow

TIMELINE_SCHEMA_VERSION = "1.0"


class Timeline:
    def __init__(self, publisher=None, topic: str = "triplet/timeline/guardian", maxlen: int = 2000):
        self.publisher, self.topic = publisher, topic
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, kind: str, device_id: str | None, summary: str, severity: str = "info",
            ref: str | None = None) -> dict:
        entry = {"schema_version": TIMELINE_SCHEMA_VERSION, "layer": "guardian",
                 "id": next(self._ids), "ts": iso(utcnow()), "kind": kind,
                 "device_id": device_id, "severity": severity, "summary": summary, "ref": ref}
        with self._lock:
            self._buf.append(entry)
        if self.publisher is not None:
            self.publisher.publish(self.topic, entry, qos=0)
        return entry

    def list(self, since: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        if since:
            items = [e for e in items if e["ts"] > since]
        return items[-limit:]
