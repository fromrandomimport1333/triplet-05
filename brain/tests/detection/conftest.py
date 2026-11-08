"""Broker-free harness for the detection module: a simulated city fed tick by tick.

The city comes from mocks/city.yaml (as brain/internal/meta would carry it); readings get
the mock's uniform noise, and scenarios override values per (zone, sensor type).
"""

from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from src.common import config
from src.common.log import EventLogger
from src.detection.service import DetectionService

CITY = yaml.safe_load((Path(config.BRAIN_ROOT) / "mocks" / "city.yaml").read_text(encoding="utf-8"))
T0 = 1_800_000_000.0
TOPICS = config.load("topics")["internal"]


def build_meta() -> dict[str, Any]:
    sensors = []
    for z in CITY["zones"]:
        nn = z["zone_id"].split("-")[-1]
        for stype, spec in CITY["sensor_types"].items():
            if spec.get("zones") is not None and z["zone_id"] not in spec["zones"]:
                continue                       # as mocks/twin/city.py: only in the zones listed
            d = {"sensor_id": f"{spec['prefix']}-{nn}", "zone_id": z["zone_id"], "type": stype,
                 "unit": spec["unit"], "min": spec["min"], "max": spec["max"]}
            if "alert" in spec:
                d["alert"] = spec["alert"]
            sensors.append(d)
    return {"id": "in-meta", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-meta",
            "zones": CITY["zones"], "sensors": sensors, "actuators": CITY["actuators"], "units": CITY["units"]}


META = build_meta()
BASELINE = {(s["zone_id"], s["type"]): CITY["sensor_types"][s["type"]]["baseline"] for s in META["sensors"]}


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
                out.append(json.loads(doc["body"]) if "body" in doc else doc)
        return out

    def incidents(self, **match) -> list[dict]:
        return [i for i in self.on(TOPICS["incident"]["topic"]) if all(i.get(k) == v for k, v in match.items())]

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.on("brain/event") if name is None or e["event"] == name]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class City:
    """Feeds the service one reading per sensor per tick (1 s), like the Twin mock."""

    def __init__(self, service: DetectionService, clock: Clock, seed: int = 1, noise: float = 0.02):
        self.s, self.clock = service, clock
        self.rng = random.Random(seed)
        self.noise = noise
        self.n = 0
        self.n_calls = 0
        self.overrides: dict[tuple[str, str], Callable[[int], float] | float] = {}
        self.factors: Callable[[int, str, str], float] | None = None   # (tick, zone, type) -> factor
        self.silent: set[str] = set()                                 # sensor ids that send nothing
        self.s.handle(TOPICS["meta"]["topic"], json.dumps(META), True)

    def trust(self, sensor_id: str, verdict: str, score: float | None = None) -> None:
        msg = {"id": f"in-t-{sensor_id}", "timestamp": iso(self.clock.t), "correlation_id": "x",
               "sensor_id": sensor_id, "verdict": verdict, "trust_score": score, "reason": "test"}
        self.s.handle(TOPICS["trust"]["topic"].format(sensor_id=sensor_id), json.dumps(msg), True)

    def trust_all(self, verdict: str = "trusted") -> None:
        for d in META["sensors"]:
            self.trust(d["sensor_id"], verdict)

    def guardian(self, status: str) -> None:
        msg = {"id": "in-ls", "timestamp": iso(self.clock.t), "layer": "guardian", "status": status}
        self.s.handle(TOPICS["layer_status"]["topic"].format(layer="guardian"), json.dumps(msg), True)

    def call(self, zone: str, category: str, text: str | None = None) -> str:
        self.n_calls += 1
        cid = f"call-{self.n_calls:04d}"
        msg = {"id": f"in-c{self.n_calls}", "timestamp": iso(self.clock.t), "correlation_id": f"twin-c{self.n_calls}",
               "call_id": cid, "zone_id": zone, "category": category, "text": text}
        self.s.handle(TOPICS["call"]["topic"], json.dumps(msg))
        return cid

    def step(self, ticks: int = 1) -> None:
        for _ in range(ticks):
            self.n += 1
            self.clock.t += 1.0
            for d in META["sensors"]:
                if d["sensor_id"] in self.silent:
                    continue
                key = (d["zone_id"], d["type"])
                o = self.overrides.get(key)
                base = (o(self.n) if callable(o) else o) if o is not None else BASELINE[key]
                if self.factors and o is None:
                    base *= self.factors(self.n, d["zone_id"], d["type"])
                value = round(base * (1 + self.rng.uniform(-self.noise, self.noise)), 3)
                value = max(d["min"], min(d["max"], value))
                msg = {"id": f"in-{self.n}-{d['sensor_id']}", "timestamp": iso(self.clock.t),
                       "correlation_id": f"twin-{self.n}-{d['sensor_id']}", "sensor_id": d["sensor_id"],
                       "zone_id": d["zone_id"], "type": d["type"], "value": value, "unit": d["unit"]}
                self.s.handle(TOPICS["reading"]["topic"], json.dumps(msg))
            self.s.tick()


@pytest.fixture
def det_cfg() -> dict[str, Any]:
    cfg = copy.deepcopy(config.load("detection"))
    cfg["logging"] = {"file": None, "level": "ERROR"}
    return cfg


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def make_city(det_cfg, clock, bus):
    def make(seed: int = 1, cfg: dict | None = None, **kw) -> City:
        service = DetectionService(bus, clock=clock, det_cfg=cfg or det_cfg,
                                   logger=EventLogger("test-detection", None, "ERROR"), **kw)
        return City(service, clock, seed)
    return make
