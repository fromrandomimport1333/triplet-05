"""Core outputs -> wire messages (topic, body, qos, retain), bodies as in the interface card.

Bodies carry `id`, `timestamp`, `source: twin` (+ `correlation_id`, `seq` where they apply).
Topic names are filled from the Brain's topics.yaml so both sides stay in step.
"""

from __future__ import annotations

from typing import Any, Callable

from src.common.ids import IdGenerator

Message = tuple[str, dict[str, Any], int, bool]   # topic, body, qos, retain


class Mapper:
    def __init__(self, topics_cfg: dict[str, Any], run_id: str, now_iso: Callable[[], str]):
        self.patterns = {k: v for k, v in topics_cfg["external_in"].items() if v["source"] == "twin"}
        self.ids = IdGenerator(run_id)
        self.now_iso = now_iso

    def _topic(self, kind: str, **fields: str) -> tuple[str, int]:
        route = self.patterns[kind]
        return route["pattern"].format(**fields), int(route["qos"])

    def _base(self, **extra: Any) -> dict[str, Any]:
        return {"id": self.ids.next(), "timestamp": self.now_iso(), "source": "twin", **extra}

    def status(self, status: str) -> Message:
        topic, qos = self._topic("twin_status")
        return topic, self._base(status=status), qos, True

    def meta(self, meta: dict[str, Any]) -> Message:
        topic, qos = self._topic("meta")
        return topic, self._base(**meta), qos, True

    def map(self, out: dict[str, Any]) -> Message:
        return getattr(self, f"_{out['kind']}")(out)

    def _reading(self, out: dict[str, Any]) -> Message:
        r = out["reading"]
        topic, qos = self._topic("reading", zone_id=r["zone_id"], type=r["type"], sensor_id=r["sensor_id"])
        body = {"id": r["reading_id"], "timestamp": r["observed_at"], "source": "twin"}
        body.update({k: r[k] for k in ("sensor_id", "zone_id", "type", "value", "unit", "origin") if k in r})
        body["seq"] = r["sequence"]
        return topic, body, qos, False

    def _call(self, out: dict[str, Any]) -> Message:
        topic, qos = self._topic("call", zone_id=out["zone_id"])
        body = self._base(call_id=out["call_id"], zone_id=out["zone_id"], category=out["category"])
        if out.get("text"):
            body["text"] = out["text"]
        return topic, body, qos, False

    def _actuator(self, out: dict[str, Any]) -> Message:
        topic, qos = self._topic("actuator", actuator_id=out["actuator_id"])
        body = self._base(actuator_id=out["actuator_id"])
        for k in ("correlation_id", "result", "state", "reason"):
            if out.get(k) is not None:
                body[k] = out[k]
        return topic, body, qos, True

    def _unit(self, out: dict[str, Any]) -> Message:
        topic, qos = self._topic("unit", unit_id=out["unit_id"])
        body = self._base(unit_id=out["unit_id"])
        for k in ("correlation_id", "result", "status", "zone_id", "reason"):
            if out.get(k) is not None:
                body[k] = out[k]
        return topic, body, qos, True
