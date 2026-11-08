from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.common import config
from src.common.log import EventLogger
from src.output.service import OutputService

TWIN_KEY = b"test-secret-twin-brain"
GUARDIAN_KEY = b"test-secret-guardian-brain"
KEYS = {"twin": TWIN_KEY, "guardian": GUARDIAN_KEY}
T0 = 1_800_000_000.0

META = {
    "id": "in-000001", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-meta-1",
    "zones": [{"zone_id": "zone-01", "name": "Port", "adjacent": ["zone-02"]},
              {"zone_id": "zone-02", "name": "Docks", "adjacent": ["zone-01"]}],
    "sensors": [
        {"sensor_id": "water-01", "zone_id": "zone-01", "type": "water_level", "unit": "m", "min": 0, "max": 5},
        {"sensor_id": "water-02", "zone_id": "zone-02", "type": "water_level", "unit": "m", "min": 0, "max": 5},
        {"sensor_id": "air-01", "zone_id": "zone-01", "type": "air_quality", "unit": "ug/m3", "min": 0, "max": 1000},
        {"sensor_id": "power-01", "zone_id": "zone-01", "type": "power_load", "unit": "kW", "min": 0, "max": 2000},
        {"sensor_id": "traffic-speed-01", "zone_id": "zone-01", "type": "traffic_speed", "unit": "km/h",
         "min": 0, "max": 200},
    ],
    "actuators": [
        {"actuator_id": "valve-01", "type": "valve", "zone_id": "zone-01", "controls": ["zone-01"],
         "actions": ["open", "close"]},
        {"actuator_id": "signal-01", "type": "traffic_signal", "zone_id": "zone-01", "controls": ["zone-01"],
         "actions": ["normal", "priority"]},
        {"actuator_id": "grid-01", "type": "power_grid", "zone_id": "zone-01", "controls": ["zone-01"],
         "actions": ["normal", "shed"]},
    ],
    "units": [{"unit_id": "unit-fire-01", "type": "fire_truck", "home_zone": "zone-02"}],
}


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Bus:
    def __init__(self):
        self.sent: list[tuple[str, str, int, bool]] = []

    def __call__(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))

    def on(self, prefix: str) -> list[dict]:
        out = []
        for t, payload, _, _ in self.sent:
            if t.startswith(prefix):
                doc = json.loads(payload)
                out.append(json.loads(doc["body"]) if "body" in doc else doc)
        return out

    def raw(self, prefix: str) -> list[tuple[str, str, int, bool]]:
        return [s for s in self.sent if s[0].startswith(prefix)]

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.on("brain/event") if name is None or e["event"] == name]

    def outcomes(self, status: str | None = None) -> list[dict]:
        return [o for o in self.on("brain/internal/outcome") if status is None or o["status"] == status]

    def clear(self):
        self.sent.clear()


class Harness:
    def __init__(self, service: OutputService, bus: Bus, clock: Clock):
        self.s, self.bus, self.clock = service, bus, clock
        self._n = 0

    def send(self, topic: str, msg: dict[str, Any], retained: bool = False) -> None:
        self.s.handle(topic, json.dumps(msg), retained)

    def nid(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:06d}"

    def meta(self) -> None:
        self.send("brain/internal/meta", META, retained=True)

    def trust(self, sensor_id: str, verdict: str = "trusted", score: float | None = 1.0) -> None:
        self.send(f"brain/internal/trust/{sensor_id}", {
            "id": self.nid("in"), "timestamp": "t", "correlation_id": "c", "sensor_id": sensor_id,
            "verdict": verdict, "trust_score": score, "reason": "ok"}, retained=True)

    def quarantine(self, device_id: str, state: str = "quarantined") -> None:
        self.send(f"brain/internal/quarantine/{device_id}", {
            "id": self.nid("in"), "timestamp": "t", "correlation_id": "c", "device_id": device_id,
            "state": state, "reason": "inconsistent"}, retained=True)

    def layer(self, layer: str, status: str) -> None:
        self.send(f"brain/internal/layer-status/{layer}", {
            "id": self.nid("in"), "timestamp": "t", "layer": layer, "status": status}, retained=True)

    def incident(self, evidence: list[dict], *, incident_id: str = "inc-0001", type_: str = "flood",
                 zone_id: str = "zone-01", status: str = "opened") -> None:
        self.send("brain/internal/incident", {
            "id": self.nid("det"), "timestamp": "t", "correlation_id": "twin-000182",
            "incident_id": incident_id, "type": type_, "zone_id": zone_id, "status": status,
            "confidence": 0.8, "severity": "high", "verified": True, "evidence": evidence})

    def decision(self, *actions: tuple[str, str, dict], decision_id: str | None = None,
                 incident_id: str = "inc-0001", mode: str = "normal", evidence_sensors=None) -> str:
        did = decision_id or self.nid("math")
        self.send("brain/internal/decision", {
            "id": did, "timestamp": "t", "correlation_id": "twin-000182", "incident_id": incident_id,
            "chosen_proposal": "llm-000001", "origin": "llm",
            "actions": [{"target_id": t, "action": a, "params": p} for t, a, p in actions],
            "score": 1.5, "alternatives": [{"proposal_id": "rules-000001", "origin": "rules", "score": 1.2}],
            "risk": "high", "evidence_sensors": evidence_sensors or [], "mode": mode,
            "explanation": "test"})
        return did

    def reading(self, sensor_id: str, zone_id: str, type_: str, value: float) -> None:
        self.send("brain/internal/reading", {
            "id": self.nid("in"), "timestamp": "t", "correlation_id": "c", "sensor_id": sensor_id,
            "zone_id": zone_id, "type": type_, "value": value, "unit": "x"})

    def ack(self, command_id: str, target_id: str, result: str = "done", *, kind: str = "actuator",
            reason: str | None = None, state: str | None = "open") -> None:
        msg = {"id": self.nid("in"), "timestamp": "t", "correlation_id": command_id,
               "message_id": self.nid("twin"), "kind": kind, "target_id": target_id, "result": result}
        if state:
            msg["state"] = state
        if reason:
            msg["reason"] = reason
        self.send("brain/internal/ack", msg)

    def advance(self, seconds: float, step: float = 0.5) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.t = min(end, self.clock.t + step)
            self.s.tick()

    def commands(self) -> list[dict]:
        return self.bus.on("brain/cmd/")


WATER2 = [{"source_id": "water-01", "kind": "reading", "value": 1.8, "unit": "m", "trust": 1.0},
          {"source_id": "call-1", "kind": "call", "category": "flood", "trust": 1.0}]


@pytest.fixture
def output_cfg() -> dict[str, Any]:
    cfg = copy.deepcopy(config.load("output"))
    cfg["logging"] = {"file": None, "level": "CRITICAL"}
    cfg["commands"]["id_start"] = 42
    return cfg


@pytest.fixture
def safety_cfg() -> dict[str, Any]:
    return copy.deepcopy(config.load("safety"))


@pytest.fixture
def make(output_cfg, safety_cfg):
    def build(*, with_meta: bool = True, keys=KEYS, **kw) -> Harness:
        clock, bus = Clock(), Bus()
        s = OutputService(bus, seal_keys=keys, clock=clock, logger=EventLogger("test-output", None, "CRITICAL"),
                          output_cfg=kw.pop("cfg", output_cfg), safety_cfg=kw.pop("safety", safety_cfg), **kw)
        h = Harness(s, bus, clock)
        if with_meta:
            h.meta()
        return h
    return build


@pytest.fixture
def h(make) -> Harness:
    return make()
