from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.common.envelope import seal, seal_multi
from src.common.log import EventLogger
from src.input.service import InputService

TWIN_KEY = b"test-secret-twin-brain"
GUARDIAN_KEY = b"test-secret-guardian-brain"
T0 = datetime(2026, 11, 14, 10, 22, 0, tzinfo=timezone.utc).timestamp()

META = {
    "id": "twin-meta-001", "source": "twin",
    "zones": [
        {"zone_id": "port", "name": "Port", "type": "port", "exposure": "high", "adjacent": ["z3"]},
        {"zone_id": "z3", "adjacent": ["port"]},
    ],
    "sensors": [
        {"sensor_id": "water-level-04", "zone_id": "port", "type": "water_level",
         "unit": "m", "min": 0.0, "max": 4.0, "alert": 2.0},
        {"sensor_id": "smoke-z3-1", "zone_id": "z3", "type": "smoke", "unit": "0-1", "min": 0, "max": 1},
    ],
    "actuators": [
        {"actuator_id": "valve-port-up", "type": "valve", "zone_id": "port",
         "controls": ["port"], "actions": ["open", "close"]},
    ],
    "units": [{"unit_id": "ambulance-2", "type": "ambulance", "home_zone": "z3"}],
}


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def iso(self, offset_s: float = 0.0) -> str:
        ts = datetime.fromtimestamp(self.t + offset_s, timezone.utc)
        return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Bus:
    """Captures what the service publishes."""

    def __init__(self):
        self.sent: list[tuple[str, str, int, bool]] = []

    def __call__(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))

    def on(self, prefix: str) -> list[dict]:
        out = []
        for topic, payload, _, _ in self.sent:
            if topic.startswith(prefix):
                doc = json.loads(payload)
                out.append(json.loads(doc["body"]) if "body" in doc else doc)
        return out

    def topics(self) -> list[str]:
        return [t for t, *_ in self.sent]

    def clear(self):
        self.sent.clear()


class Harness:
    def __init__(self, clock: Clock, bus: Bus, service: InputService):
        self.clock, self.bus, self.service = clock, bus, service
        self._n = 0

    def body(self, **fields) -> dict:
        self._n += 1
        base = {"id": f"twin-{self._n:06d}", "timestamp": self.clock.iso(), "source": "twin"}
        return {**base, **fields}

    def send(self, topic: str, body: dict, key: bytes = TWIN_KEY, retained=False, multi=False):
        payload = seal_multi(body, {"brain": key, "guardian": b"x" * 32}) if multi else seal(body, key)
        self.service.handle(topic, payload, retained)

    def send_meta(self, retained=True):
        self.send("twin/meta", {**META, "timestamp": self.clock.iso()}, retained=retained)

    def reading(self, value=2.4, sensor="water-level-04", zone="port", type_="water_level", unit="m", **extra):
        return self.body(sensor_id=sensor, zone_id=zone, type=type_, value=value, unit=unit, **extra)

    def send_reading(self, value=2.4, **kw):
        body = self.reading(value, **kw)
        self.send(f"city/{body['zone_id']}/sensor/{body['type']}/{body['sensor_id']}", body)
        return body

    def guardian(self, **fields) -> dict:
        self._n += 1
        return {"id": f"guardian-{self._n:06d}", "timestamp": self.clock.iso(), "source": "guardian", **fields}

    def errors(self) -> list[dict]:
        return self.bus.on("brain/error")

    def last_error_code(self) -> str | None:
        errs = self.errors()
        return errs[-1]["code"] if errs else None


@pytest.fixture
def h(tmp_path) -> Harness:
    clock, bus = Clock(), Bus()
    logger = EventLogger(f"input-test-{id(tmp_path)}", str(tmp_path / "input.jsonl"), "INFO")
    service = InputService(
        bus, hmac_keys={"twin": TWIN_KEY, "guardian": GUARDIAN_KEY}, clock=clock, logger=logger
    )
    return Harness(clock, bus, service)


@pytest.fixture
def ready(h) -> Harness:
    """Harness with the city description already loaded."""
    h.send_meta()
    h.bus.clear()
    return h
