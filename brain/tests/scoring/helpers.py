"""Broker-free helpers for the scoring tests and the worked example: fake bus, fake clock,
the mock city as a brain/internal/meta message, and message builders."""

from __future__ import annotations

import itertools
import json
from typing import Any

from mocks.twin.city import load_city

T0 = 1_800_000_000.0
_ids = itertools.count(1)


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

    def on(self, topic: str) -> list[dict]:
        out = []
        for t, payload, _, _ in self.sent:
            if t == topic:
                doc = json.loads(payload)
                out.append(json.loads(doc["body"]) if "body" in doc and "v" in doc else doc)
        return out

    def decisions(self, incident_id: str | None = None) -> list[dict]:
        return [d for d in self.on("brain/internal/decision") if incident_id in (None, d["incident_id"])]

    def proposals(self, origin: str = "rules", incident_id: str | None = None) -> list[dict]:
        return [p for p in self.on("brain/internal/proposal")
                if p["origin"] == origin and incident_id in (None, p["incident_id"])]

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.on("brain/event") if name is None or e["event"] == name]


def meta_message() -> dict[str, Any]:
    """brain/internal/meta for the mock city (mocks/city.yaml)."""
    city = load_city()
    return {
        "id": "in-000001", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-000001",
        "zones": [{k: z[k] for k in ("zone_id", "name", "type", "exposure", "adjacent") if k in z} for z in city.zones],
        "sensors": [{"sensor_id": sid, "zone_id": s["zone_id"], "type": s["type"], "unit": s["unit"],
                     "min": s["min"], "max": s["max"], **({"alert": s["alert"]} if s.get("alert") is not None else {})}
                    for sid, s in city.sensors.items()],
        "actuators": [{"actuator_id": aid, **a} for aid, a in city.actuators.items()],
        "units": [{"unit_id": uid, **u} for uid, u in city.units.items()],
    }


def reading(sensor_id: str, trust: float = 1.0, value: float = 2.0) -> dict[str, Any]:
    return {"source_id": sensor_id, "kind": "reading", "value": value, "unit": "m", "trust": trust}


def call(call_id: str, category: str, trust: float = 1.0) -> dict[str, Any]:
    return {"source_id": call_id, "kind": "call", "category": category, "trust": trust}


def incident(incident_id: str, type_: str, zone: str, *, severity: str = "medium", confidence: float = 0.8,
             status: str = "opened", verified: bool = True, evidence: list[dict] | None = None) -> dict[str, Any]:
    if evidence is None:
        n = zone.split("-")[-1]
        evidence = {"flood": [reading(f"water-{n}")],
                    "fire": [reading(f"air-{n}"), call("call-1", "fire")],
                    "accident": [reading(f"traffic-speed-{n}"), call("call-2", "accident")],
                    "traffic_jam": [reading(f"traffic-density-{n}")],
                    "power_problem": [reading(f"power-{n}")]}[type_]
    return {"id": f"det-{next(_ids):06d}", "timestamp": "2026-11-14T10:00:01.000Z",
            "correlation_id": f"twin-{incident_id}", "incident_id": incident_id, "type": type_, "zone_id": zone,
            "status": status, "confidence": confidence, "severity": severity, "verified": verified,
            "evidence": evidence}


def proposal(incident_id: str, actions: list[tuple], origin: str = "llm", pid: str | None = None) -> dict[str, Any]:
    acts = [{"target_id": a[0], "action": a[1], "params": a[2] if len(a) > 2 else {}} for a in actions]
    return {"id": pid or f"llm-{next(_ids):06d}", "timestamp": "2026-11-14T10:00:02.000Z",
            "correlation_id": f"twin-{incident_id}", "incident_id": incident_id, "origin": origin,
            "actions": acts, "justification": "test"}


def unit_ack(unit_id: str, status: str, zone: str | None = None) -> dict[str, Any]:
    msg = {"id": f"in-{next(_ids):06d}", "timestamp": "2026-11-14T10:00:03.000Z", "correlation_id": "twin-x",
           "message_id": "twin-x", "kind": "unit", "target_id": unit_id, "status": status}
    if zone:
        msg["zone_id"] = zone
    return msg


def outcome(incident_id: str, decision_id: str, target_id: str, action: str, status: str = "failed",
            rule: str | None = None) -> dict[str, Any]:
    msg = {"id": f"out-{next(_ids):06d}", "timestamp": "2026-11-14T10:00:04.000Z",
            "correlation_id": f"twin-{incident_id}", "incident_id": incident_id, "decision_id": decision_id,
            "target_id": target_id, "action": action, "status": status, "detail": "test"}
    if rule:
        msg["rule"] = rule
    return msg


def send(svc, topic: str, msg: dict[str, Any], retained: bool = False) -> None:
    svc.handle(topic, json.dumps(msg), retained)


def actions_of(decision: dict) -> list[tuple[str, str]]:
    return [(a["target_id"], a["action"]) for a in decision["actions"]]
