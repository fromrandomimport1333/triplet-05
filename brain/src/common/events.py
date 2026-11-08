"""Timeline events (brain/event, BR-3): built, sealed and published the same way by every module."""

from __future__ import annotations

from typing import Any, Callable

from .envelope import seal_for
from .ids import IdGenerator
from .log import utc_now_iso
from .models import TimelineEvent

Publisher = Callable[[str, str, int, bool], None]


def publish_event(publish: Publisher, topics: dict[str, Any], ids: IdGenerator, keys: dict[str, bytes] | None,
                  event: str, summary: str, *, correlation_id: str | None = None,
                  incident_id: str | None = None, timestamp: str | None = None) -> None:
    body = TimelineEvent(id=ids.next(), timestamp=timestamp or utc_now_iso(), event=event, summary=summary,
                         correlation_id=correlation_id, incident_id=incident_id).model_dump(exclude_none=True)
    spec = topics["external_out"]["event"]
    publish(spec["topic"], seal_for(body, keys), int(spec["qos"]), bool(spec["retain"]))
