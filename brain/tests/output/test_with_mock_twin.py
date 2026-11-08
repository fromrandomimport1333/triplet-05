"""Output module against the real Twin mock and the real input module, on one clock (no broker).

Twin --(sealed)--> Input --(brain/internal/*)--> Output --(brain/cmd, sealed)--> Twin
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from mocks.twin.service import TwinService
from src.common import config
from src.common.log import EventLogger
from src.input.service import InputService
from src.output.service import OutputService

from .conftest import KEYS, TWIN_KEY, Bus, Clock

T0 = datetime(2026, 11, 14, 10, 22, 0, tzinfo=timezone.utc).timestamp()


class Loop:
    def __init__(self, tmp_path, modes=None, **sim):
        self.clock = Clock(T0)
        cfg = copy.deepcopy(config.load("twin-mock"))
        cfg["sim"].update({"scenario": "flood", "scenario_zone": "zone-01", "scenario_after_ticks": 1, **sim})
        cfg["actuator_modes"] = modes or {}
        cfg["faults"] = []
        self.twin_bus, self.in_bus, self.out_bus = Bus(), Bus(), Bus()
        self.twin = TwinService(self.twin_bus, key=TWIN_KEY, clock=self.clock, twin_cfg=cfg,
                                logger=EventLogger(f"lt-twin-{id(self)}", None, "CRITICAL"))
        self.input = InputService(self.in_bus, hmac_keys=KEYS, clock=self.clock,
                                  logger=EventLogger(f"lt-in-{id(self)}", None, "CRITICAL"))
        ocfg = copy.deepcopy(config.load("output"))
        ocfg["logging"] = {"file": None}
        self.output = OutputService(self.out_bus, seal_keys=KEYS, clock=self.clock, output_cfg=ocfg,
                                    logger=EventLogger(f"lt-out-{id(self)}", None, "CRITICAL"))
        self._fed = {"twin": 0, "in": 0, "out": 0}

    def pump(self) -> None:
        for _ in range(5):
            moved = False
            for topic, payload, _q, retain in self.twin_bus.sent[self._fed["twin"]:]:
                self.input.handle(topic, payload, retain)
                moved = True
            self._fed["twin"] = len(self.twin_bus.sent)
            for topic, payload, _q, retain in self.in_bus.sent[self._fed["in"]:]:
                if topic.startswith("brain/internal/"):
                    self.output.handle(topic, payload, retain)
                    moved = True
            self._fed["in"] = len(self.in_bus.sent)
            for topic, payload, _q, _r in self.out_bus.sent[self._fed["out"]:]:
                if topic.startswith("brain/cmd/"):
                    self.twin.handle(topic, payload)
                    moved = True
            self._fed["out"] = len(self.out_bus.sent)
            if not moved:
                return

    def connect(self) -> None:
        self.twin.on_connect()
        self.pump()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock.t += 1
            self.twin.tick()
            self.pump()
            self.output.tick()
            self.pump()

    def decide(self, target: str, action: str, params=None, sensors=("water-01", "power-01")) -> None:
        self.output.handle("brain/internal/decision", json.dumps({
            "id": f"math-{int(self.clock.t)}", "timestamp": "t", "correlation_id": "twin-x",
            "incident_id": "inc-1", "chosen_proposal": "llm-1", "origin": "llm",
            "actions": [{"target_id": target, "action": action, "params": params or {}}],
            "score": 1.5, "risk": "high", "evidence_sensors": list(sensors), "mode": "normal",
            "explanation": "test"}))
        self.pump()

    def outcomes(self) -> list[dict]:
        return [json.loads(p) for t, p, *_ in self.out_bus.sent if t == "brain/internal/outcome"]

    def alerts(self) -> list[str]:
        return [json.loads(json.loads(p)["body"])["alert"] for t, p, *_ in self.out_bus.sent if t == "brain/alert"]


@pytest.fixture
def make_loop(tmp_path):
    return lambda **kw: Loop(tmp_path, **kw)


def test_valve_is_accepted_acknowledged_and_has_effect(make_loop):
    lp = make_loop()
    lp.connect()
    lp.tick(8)                                   # flood rising in zone-01
    lp.decide("valve-01", "open", {"opening": 1.0})
    assert [o["status"] for o in lp.outcomes()] == ["sent"]
    lp.tick(4)
    assert [o["status"] for o in lp.outcomes()] == ["sent", "acknowledged"]
    lp.tick(5)
    events = [json.loads(json.loads(p)["body"])["event"] for t, p, *_ in lp.out_bus.sent if t == "brain/event"]
    assert "effect_confirmed" in events
    assert lp.alerts() == []


def test_stuck_valve_is_failed_no_effect(make_loop):
    lp = make_loop(modes={"valve-01": "stuck"})
    lp.connect()
    lp.tick(8)
    lp.decide("valve-01", "open", {"opening": 1.0})
    lp.tick(40)
    statuses = [o["status"] for o in lp.outcomes()]
    assert statuses == ["sent", "acknowledged", "failed"]
    assert lp.outcomes()[-1]["detail"] == "no_effect"
    assert lp.alerts() == ["actuator_no_effect"]


def test_actuator_without_ack_is_retried_then_failed(make_loop):
    lp = make_loop(modes={"valve-01": "no_ack"})
    lp.connect()
    lp.tick(3)
    lp.decide("valve-01", "open")
    lp.tick(10)
    assert [o["status"] for o in lp.outcomes()] == ["sent", "failed"]
    assert lp.outcomes()[-1]["detail"] == "no_ack"
    assert lp.alerts() == ["actuator_not_responding"]
    assert len([1 for t, *_ in lp.out_bus.sent if t == "brain/cmd/valve-01"]) == 2


def test_busy_unit_is_failed_with_reason(make_loop):
    lp = make_loop(scenario="calm")
    lp.connect()
    lp.tick(2)
    lp.decide("unit-fire-01", "dispatch", {"zone_id": "zone-01"}, sensors=("air-01", "water-01"))
    lp.output._last_sent.clear()                 # bypass the cooldown to send a second dispatch
    lp.output.handle("brain/internal/decision", json.dumps({
        "id": "math-second", "timestamp": "t", "correlation_id": "twin-y", "incident_id": "inc-2",
        "chosen_proposal": "p", "origin": "rules",
        "actions": [{"target_id": "unit-fire-01", "action": "dispatch", "params": {"zone_id": "zone-02"}}],
        "score": 1, "risk": "high", "evidence_sensors": ["air-01", "water-01"], "mode": "normal",
        "explanation": "x"}))
    lp.pump()
    lp.tick(4)
    failed = [o for o in lp.outcomes() if o["status"] == "failed"]
    assert failed and failed[0]["detail"] == "unit_busy"


def test_valve_effect_is_seen_while_the_flood_is_still_rising(make_loop):
    """B2 timing: the command goes out while the water is still rising (not yet at its ceiling)."""
    for seed in (1, 2, 3, 7):
        lp = make_loop(scenario_after_ticks=5, seed=seed)
        lp.connect()
        lp.tick(8)
        lp.decide("valve-01", "open", {"opening": 1.0})
        lp.tick(40)
        assert [o["status"] for o in lp.outcomes()] == ["sent", "acknowledged"], seed
        assert lp.alerts() == [], seed
