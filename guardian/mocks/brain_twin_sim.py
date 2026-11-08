"""SYNTHETIC stand-in for the Brain team's mock Twin, speaking the kit's protocol.

It is NOT the Brain repository's mock Twin (brain/mocks/twin/), which was not available.
Its city and dynamics are derived only from figures written in the integration kit:
6 zones, 6 sensor types per zone (36 sensors), one reading per sensor per second (Test Book
B1), water-01 at 0.4 m rising ~0.15 m/s in a flood (B2), a fake 1.8 m from 10 s (B3/T5),
the six bad-message kinds of B9, kit units (traffic_flow in vehicles/min). Noise amplitudes
are our assumptions. Used by tests and scenarios G6-G9 in local mode.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from src.adapter.envelope import iso_ms, seal, seal_multi

ZONES = ["zone-01", "zone-02", "zone-03", "zone-04", "zone-05", "zone-06"]
TYPES = {  # type: (id prefix, unit, min, max, alert, calm value, noise)
    "traffic_speed": ("traffic-speed", "km/h", 0, 130, None, 45.0, 1.5),
    "traffic_density": ("traffic-density", "vehicles/km", 0, 200, 150, 20.0, 1.0),
    "traffic_flow": ("traffic-flow", "vehicles/min", 0, 100, None, 15.0, 0.8),
    "water_level": ("water", "m", 0, 5, 1.3, 0.4, 0.02),
    "power_load": ("power", "kW", 0, 5000, 4000, 800.0, 15.0),
    "air_quality": ("air", "ug/m3", 0, 500, 150, 30.0, 2.0),
}


def sensor_id(t: str, zone: str) -> str:
    return f"{TYPES[t][0]}-{zone[-2:]}"


class BrainTwinSim:
    def __init__(self, twin_guardian_key: str, twin_brain_key: str = "brain-side-key",
                 start: datetime | None = None, run: str = "r1", seed: int = 7) -> None:
        self.kg, self.kb = twin_guardian_key, twin_brain_key
        self.t0 = start or datetime.now(timezone.utc)
        self.t = 0.0           # seconds since start
        self.run = run
        self.rng = random.Random(seed)
        self.n = 0
        self.seq: dict[str, int] = {}
        self.water01 = 0.4

    # --------------------------------------------------------------- clock / ids
    def now(self) -> datetime:
        return self.t0 + timedelta(seconds=self.t)

    def _id(self) -> str:
        self.n += 1
        return f"tw-{self.run}-{self.n:06d}"

    def _env(self, body: dict) -> str:
        return seal_multi(body, {"brain": self.kb, "guardian": self.kg})

    # --------------------------------------------------------------- meta
    def meta_body(self) -> dict:
        sensors = []
        for z in ZONES:
            for t, (_, unit, lo, hi, alert, *_r) in TYPES.items():
                sensors.append({"sensor_id": sensor_id(t, z), "zone_id": z, "type": t, "unit": unit,
                                "min": lo, "max": hi, "alert": alert})
        return {"id": self._id(), "timestamp": iso_ms(self.now()), "source": "twin",
                "zones": [{"zone_id": z, "name": z, "type": "district", "exposure": "medium",
                           "adjacent": []} for z in ZONES],
                "sensors": sensors,
                "actuators": [{"actuator_id": "valve-01", "type": "valve", "zone_id": "zone-01",
                               "controls": ["zone-01"], "actions": ["open", "close"]},
                              {"actuator_id": "signal-01", "type": "traffic_signal", "zone_id": "zone-01",
                               "controls": ["zone-01"], "actions": ["normal", "priority"]}],
                "units": [{"unit_id": "unit-ambulance-01", "type": "ambulance", "home_zone": "zone-04"},
                          {"unit_id": "unit-ambulance-02", "type": "ambulance", "home_zone": "zone-04"},
                          {"unit_id": "unit-rescue-01", "type": "rescue", "home_zone": "zone-02"}]}

    def meta(self, tagged: bool = True) -> tuple[str, str]:
        body = self.meta_body()
        return "twin/meta", (self._env(body) if tagged else seal(body, self.kb))

    # --------------------------------------------------------------- readings
    def reading_body(self, t: str, zone: str, value: float, *, seq: int | None = None,
                     ts: datetime | None = None, unit: str | None = None) -> dict:
        sid = sensor_id(t, zone)
        if seq is None:
            self.seq[sid] = self.seq.get(sid, 0) + 1
            seq = self.seq[sid]
        b = {"id": self._id(), "timestamp": iso_ms(ts or self.now()), "source": "twin",
             "sensor_id": sid, "zone_id": zone, "type": t, "value": round(value, 3),
             "unit": unit or TYPES[t][1], "origin": "simulated"}
        if seq is not False:
            b["seq"] = seq
        return b

    @staticmethod
    def topic(b: dict) -> str:
        return f"city/{b['zone_id']}/sensor/{b['type']}/{b['sensor_id']}"

    def value(self, t: str, zone: str, scenario: str) -> float:
        _, _, lo, hi, _, calm, noise = TYPES[t]
        v = calm
        x = self.t
        if scenario == "rush" and 10 <= x <= 50:
            bump = math.sin(math.pi * (x - 10) / 40) ** 2   # smooth 0 -> 1 -> 0
            v = {"traffic_speed": calm - 20 * bump, "traffic_density": calm * (1 + 2 * bump),
                 "traffic_flow": calm * (1 + 1.0 * bump)}.get(t, calm)
        if t == "water_level" and zone == "zone-01" and scenario == "flood":
            if 5 <= x < 45:
                self.water01 = min(4.5, self.water01 + 0.15)
            elif x >= 45:
                self.water01 = max(0.4, self.water01 - 0.1)
            v = self.water01
        if scenario == "fire" and zone == "zone-05" and x >= 5:
            v = {"air_quality": 250.0, "power_load": 1600.0}.get(t, v)
        return min(hi, max(lo, v + self.rng.uniform(-noise, noise)))

    def tick(self, scenario: str = "calm", falsify: tuple[float, float] | None = None,
             freeze: tuple[str, float] | None = None) -> list[tuple[str, str]]:
        """Advance one second and return every reading of the 36 sensors as (topic, payload).
        freeze=(sensor_id, from_t): that sensor repeats its last value from from_t (Twin 'freeze')."""
        self.t += 1.0
        self.frozen = getattr(self, "frozen", {})
        out = []
        for z in ZONES:
            for t in TYPES:
                v = self.value(t, z, scenario)
                if falsify and t == "water_level" and z == "zone-01" and falsify[0] <= self.t < falsify[1]:
                    v = 1.8
                sid = sensor_id(t, z)
                if freeze and freeze[0] == sid and self.t >= freeze[1]:
                    v = self.frozen.setdefault(sid, round(v, 3))
                b = self.reading_body(t, z, v)
                out.append((self.topic(b), self._env(b)))
        return out

    # --------------------------------------------------------------- bad messages (B9)
    def bad(self, kind: str) -> list[tuple[str, str]]:
        if kind == "malformed":       # no value
            b = self.reading_body("air_quality", "zone-02", 30.0)
            del b["value"]
            return [(self.topic(b), self._env(b))]
        if kind == "bad_hmac":
            b = self.reading_body("power_load", "zone-03", 800.0)
            return [(self.topic(b), seal_multi(b, {"brain": self.kb, "guardian": "not-the-key"}))]
        if kind == "duplicate":
            b = self.reading_body("traffic_flow", "zone-04", 15.0)
            p = self._env(b)
            return [(self.topic(b), p), (self.topic(b), p)]
        if kind == "unknown_id":
            b = self.reading_body("water_level", "zone-06", 0.4)
            b["sensor_id"] = "water-99"
            return [(self.topic(b), self._env(b))]
        if kind == "impossible":
            b = self.reading_body("water_level", "zone-05", 99.0)
            return [(self.topic(b), self._env(b))]
        if kind == "old":
            b = self.reading_body("traffic_speed", "zone-06", 45.0, ts=self.now() - timedelta(seconds=15))
            return [(self.topic(b), self._env(b))]
        raise ValueError(kind)

    # --------------------------------------------------------------- acks
    def ack(self, actuator_id: str, correlation_id: str | None, result: str = "done",
            state: str = "open_100", kind: str = "actuator") -> tuple[str, str]:
        b = {"id": self._id(), "timestamp": iso_ms(self.now()), "source": "twin",
             f"{kind}_id": actuator_id, "result": result}
        if correlation_id is not None:
            b["correlation_id"] = correlation_id
        if kind == "actuator":
            b["state"] = state
        else:
            b.update({"status": "en_route", "zone_id": "zone-01"})
        return f"twin/{kind}/{actuator_id}", self._env(b)


class BrainSim:
    """Stand-in for what the Brain sends us: commands, alerts, control (sealed for Guardian)."""

    def __init__(self, guardian_brain_key: str, clock) -> None:
        self.k, self.clock, self.n = guardian_brain_key, clock, 0

    def _id(self, p="brain") -> str:
        self.n += 1
        return f"{p}-{self.n:06d}"

    def command(self, target: str, action: str, cmd_id: str | None = None) -> tuple[str, str, str]:
        cid = cmd_id or self._id()
        b = {"id": cid, "timestamp": iso_ms(self.clock()), "source": "brain", "correlation_id": "tw-r1-000001",
             "target_id": target, "action": action, "parameters": {}}
        return f"brain/cmd/{target}", seal_multi(b, {"twin": "twin-brain-key", "guardian": self.k}), cid

    def alert(self, target: str, alert: str = "actuator_no_effect") -> tuple[str, str]:
        b = {"id": self._id(), "timestamp": iso_ms(self.clock()), "source": "brain", "alert": alert,
             "target_id": target, "detail": "water still rising 30 s after open"}
        return "brain/alert", seal_multi(b, {"guardian": self.k})

    def control(self, op: dict, key: str | None = None) -> tuple[str, str]:
        b = {"id": self._id("tester"), "timestamp": iso_ms(self.clock()), "source": "brain", **op}
        return "guardian/control", seal(b, key or self.k)
