from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from src.common import config
from src.common.envelope import seal, seal_multi
from src.common.log import EventLogger
from src.timeline.runlog import RunLog
from src.timeline.service import TimelineService

TWIN_KEY = b"test-secret-twin-brain"
GUARDIAN_KEY = b"test-secret-guardian-brain"
KEYS = {"twin": TWIN_KEY, "guardian": GUARDIAN_KEY}
T0 = datetime(2026, 11, 14, 10, 22, 0, tzinfo=timezone.utc).timestamp()

META = {
    "id": "in-000001", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-meta",
    "zones": [{"zone_id": "zone-01", "name": "Port", "adjacent": ["zone-02"]},
              {"zone_id": "zone-02", "name": "Docks", "adjacent": ["zone-01"]}],
    "sensors": [
        {"sensor_id": "water-01", "zone_id": "zone-01", "type": "water_level", "unit": "m", "min": 0, "max": 5,
         "alert": 1.3},
        {"sensor_id": "air-01", "zone_id": "zone-01", "type": "air_quality", "unit": "ug/m3", "min": 0,
         "max": 1000, "alert": 150},
    ],
    "actuators": [{"actuator_id": "valve-01", "type": "valve", "zone_id": "zone-01", "controls": ["zone-01"],
                   "actions": ["open", "close"]}],
    "units": [{"unit_id": "unit-fire-01", "type": "fire_truck", "home_zone": "zone-02"}],
}


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def iso(self) -> str:
        return datetime.fromtimestamp(self.t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Harness:
    def __init__(self, service: TimelineService, clock: Clock, logdir):
        self.s, self.clock, self.logdir = service, clock, logdir
        self._n = 0

    def nid(self, p: str) -> str:
        self._n += 1
        return f"{p}-{self._n:06d}"

    def twin(self, topic: str, body: dict[str, Any], retained: bool = False, key: bytes = TWIN_KEY):
        body = {"id": self.nid("twin"), "timestamp": self.clock.iso(), "source": "twin", **body}
        return self.s.handle(topic, seal(body, key), retained)

    def guardian(self, topic: str, body: dict[str, Any], retained: bool = False):
        body = {"id": self.nid("gd"), "timestamp": self.clock.iso(), "source": "guardian", **body}
        return self.s.handle(topic, seal(body, GUARDIAN_KEY), retained)

    def brain(self, topic: str, body: dict[str, Any], retained: bool = False):
        body = {"id": self.nid("out"), "timestamp": self.clock.iso(), "source": "brain", **body}
        return self.s.handle(topic, seal_multi(body, KEYS), retained)

    def internal(self, topic: str, body: dict[str, Any], retained: bool = False):
        body = {"id": self.nid("in"), "timestamp": self.clock.iso(), **body}
        return self.s.handle(topic, json.dumps(body), retained)

    def meta(self):
        return self.s.handle("brain/internal/meta", json.dumps(META), True)

    def lines(self, name: str | None = None) -> list[dict]:
        files = sorted(self.logdir.glob("*.log")) if name is None else [self.logdir / name]
        out = []
        for f in files:
            out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
        return out


@pytest.fixture
def timeline_cfg() -> dict[str, Any]:
    cfg = copy.deepcopy(config.load("timeline"))
    cfg["logging"] = {"file": None, "level": "CRITICAL"}
    return cfg


@pytest.fixture
def make(tmp_path, timeline_cfg):
    def build(keys=KEYS, **kw) -> Harness:
        clock = Clock()
        logdir = tmp_path / "logs"
        svc = TimelineService(clock=clock, logger=EventLogger("test-timeline", None, "CRITICAL"),
                              timeline_cfg=kw.pop("cfg", timeline_cfg), hmac_keys=keys,
                              runlog=RunLog(logdir, clock=clock), **kw)
        h = Harness(svc, clock, logdir)
        h.meta()
        return h
    return build


@pytest.fixture
def h(make) -> Harness:
    return make()
