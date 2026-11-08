from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.common import config
from src.common.envelope import seal_multi
from src.common.log import EventLogger
from src.input.service import InputService
from mocks.twin.service import TwinService

TWIN_KEY = b"test-secret-twin-brain"
GUARDIAN_KEY = b"test-secret-guardian-brain"
T0 = datetime(2026, 11, 14, 10, 22, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def iso(self, offset_s: float = 0.0) -> str:
        ts = datetime.fromtimestamp(self.t + offset_s, timezone.utc)
        return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Bus:
    def __init__(self):
        self.sent: list[tuple[str, str, int, bool]] = []

    def __call__(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))

    def bodies(self, prefix: str = "") -> list[tuple[str, dict]]:
        return [(t, json.loads(json.loads(p)["body"])) for t, p, _, _ in self.sent if t.startswith(prefix)]

    def clear(self):
        self.sent.clear()


class Rig:
    """Twin mock and the Brain's real input module on one clock; the Twin's output feeds the Brain."""

    def __init__(self, tmp_path, faults=None, **sim):
        self.clock = Clock()
        cfg = deepcopy(config.load("twin-mock"))
        cfg["sim"].update(sim)
        cfg["faults"] = faults or []
        self.twin_bus, self.brain_bus = Bus(), Bus()
        self.twin = TwinService(self.twin_bus, key=TWIN_KEY, clock=self.clock, twin_cfg=cfg,
                                logger=EventLogger(f"twin-test-{id(tmp_path)}"))
        self.brain = InputService(self.brain_bus, hmac_keys={"twin": TWIN_KEY, "guardian": GUARDIAN_KEY},
                                  clock=self.clock, logger=EventLogger(f"in-test-{id(tmp_path)}", str(tmp_path / "in.jsonl")))
        self.fed = 0

    def feed(self) -> None:
        """Hand everything the Twin published since the last call to the Brain."""
        for topic, payload, _qos, retain in self.twin_bus.sent[self.fed:]:
            self.brain.handle(topic, payload, retain)
        self.fed = len(self.twin_bus.sent)

    def connect(self) -> None:
        self.twin.on_connect()
        self.feed()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock.t += self.twin.core.config.tick_s
            self.twin.tick()
            self.feed()

    def command(self, target: str, action: str, parameters=None, cid: str = "brain-000001", age_s: float = 0.0):
        body = {"id": cid, "timestamp": self.clock.iso(-age_s), "source": "brain", "target_id": target, "action": action}
        if parameters is not None:
            body["parameters"] = parameters
        self.twin.handle(f"brain/cmd/{target}", seal_multi(body, {"twin": TWIN_KEY, "guardian": GUARDIAN_KEY}))
        self.feed()

    def errors(self) -> list[dict]:
        return [b for _, b in self.brain_bus.bodies("brain/error")]

    def internal(self, topic: str) -> list[dict]:
        return [json.loads(p) for t, p, *_ in self.brain_bus.sent if t.startswith(topic)]


@pytest.fixture
def make_rig(tmp_path):
    def make(**kw) -> Rig:
        return Rig(tmp_path, **kw)
    return make
