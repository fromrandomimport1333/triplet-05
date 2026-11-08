"""Case memory (src/memory): the live window against the NIST library, on held-out replays."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import random

import pytest

from mocks.twin.core import trace
from src.common import config
from src.common.log import EventLogger
from src.memory import service as mem

TOPICS = config.load("topics")
T0 = 1_800_000_000.0
CALM = {"smoke": 0.05, "co": 3.0, "co2": 450.0, "temperature": 22.0}
NOISE = {"smoke": 0.01, "co": 0.3, "co2": 5.0, "temperature": 0.1}
TYPES = {"smoke": "smoke", "co": "co", "co2_ppm": "co2", "temperature": "temperature"}


def meta(zone="zone-06", types=("smoke", "co", "co2", "temperature")):
    pref = {"smoke": "smoke", "co": "co", "co2": "co2", "temperature": "temp"}
    nn = zone[-2:]
    return {"id": "m", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-meta",
            "zones": [{"zone_id": zone, "name": "Residential"}],
            "sensors": [{"sensor_id": f"{pref[t]}-{nn}", "zone_id": zone, "type": t, "unit": "x", "min": 0,
                         "max": 1e5} for t in types],
            "actuators": [], "units": []}


class Harness:
    def __init__(self, cfg, library, zone="zone-06", types=("smoke", "co", "co2", "temperature")):
        self.t, self.sent, self.zone, self.types = T0, [], zone, types
        self.rng = random.Random(3)
        self.s = mem.MemoryService(lambda *a: self.sent.append(a), clock=lambda: self.t, mem_cfg=cfg,
                                   logger=EventLogger("test-memory", None, "ERROR"), library=library)
        self.s.handle(TOPICS["internal"]["meta"]["topic"], json.dumps(meta(zone, types)), True)

    def run(self, seconds: int, scenario: str | None = None, start: int = 0) -> None:
        rows = trace(scenario) if scenario else ()
        pref = {"smoke": "smoke", "co": "co", "co2": "co2", "temperature": "temp"}
        for i in range(seconds):
            self.t += 1.0
            row = rows[min(start + i, len(rows) - 1)] if rows else {}
            for col, stype in TYPES.items():
                if stype not in self.types:
                    continue
                v = CALM[stype] + row.get(col, 0.0) + self.rng.uniform(-NOISE[stype], NOISE[stype])
                msg = {"id": "r", "timestamp": "x", "correlation_id": f"twin-{i}-{stype}",
                       "sensor_id": f"{pref[stype]}-{self.zone[-2:]}", "zone_id": self.zone, "type": stype,
                       "value": round(v, 3), "unit": "x"}
                self.s.handle(TOPICS["internal"]["reading"]["topic"], json.dumps(msg))
            self.s.tick()

    def cases(self):
        return [json.loads(p) for t, p, _, _ in self.sent if t == TOPICS["internal"]["case"]["topic"]]

    def events(self):
        out = []
        for t, p, _, _ in self.sent:
            if t == "brain/event":
                doc = json.loads(p)
                out.append(json.loads(doc["body"]) if "body" in doc else doc)
        return out


@pytest.fixture(scope="module")
def cfg():
    c = copy.deepcopy(config.load("memory"))
    c["logging"] = {"file": None, "level": "ERROR"}
    return c


@pytest.fixture(scope="module")
def library(cfg):
    return mem.load_library(cfg["library"]["file"], cfg["features"], cfg["scales"])


def test_library_keeps_the_held_out_group_out(cfg, library):
    assert len(library) == 4998                       # groups 1-4 (the notebook's Qdrant upload)
    every = mem.load_library(cfg["library"]["file"], cfg["features"], cfg["scales"], include_holdout=True)
    assert len(every) == 6262
    assert {s for _, s, _ in library} == {"quiet", "nuisance", "fire"}


def test_calm_zone_says_nothing(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    assert h.cases() == [] and h.events() == []


def test_burnt_toast_is_cooking(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    h.run(30, "cooking")
    case = h.cases()[-1]
    assert case["verdict"] == "nuisance" and case["share"] >= 0.9 and case["zone_id"] == "zone-06"
    assert case["features"]["co"] < 1 and case["features"]["obsc"] > 0.5
    ev = h.events()
    assert [e["event"] for e in ev] == ["case_nuisance"]
    assert "COOKING, not a fire" in ev[0]["summary"] and "lab cases" in ev[0]["summary"]


def test_real_fire_is_a_fire(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    h.run(60, "fire")
    case = h.cases()[-1]
    assert case["verdict"] == "fire" and case["share"] >= 0.6          # first clear majority (~45 s in)
    assert max(case["sources"], key=case["sources"].get).startswith("fire")
    assert "case_fire" in [e["event"] for e in h.events()]


def test_verdict_repeats_while_it_lasts_and_clears(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    h.run(60, "cooking")
    n = len(h.cases())
    assert n >= 3                                     # every repeat_s while non-quiet
    h.run(200)                                        # smoke gone (calm again)
    assert h.cases()[-1]["verdict"] == "quiet"
    assert len([e for e in h.events() if e["event"] == "case_nuisance"]) == 1   # one row per change


def test_zone_without_co_is_not_assessed(cfg, library):
    h = Harness(cfg, library, types=("smoke", "temperature"))
    h.run(120)
    h.run(40, "cooking")
    assert h.s.zones == {} and h.cases() == []


def test_rejected_sensor_is_left_out(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    msg = {"id": "t", "timestamp": "x", "correlation_id": "x", "sensor_id": "smoke-06", "verdict": "rejected",
           "trust_score": 0.05, "reason": "implausible_jump"}
    h.s.handle(TOPICS["internal"]["trust"]["topic"].format(sensor_id="smoke-06"), json.dumps(msg), True)
    h.run(40, "cooking")
    assert h.cases() == []


def test_features_match_the_build_tool(library):
    """src/memory and tools/build_cases.py compute the same 7 numbers (the library's encoding)."""
    spec = importlib.util.spec_from_file_location("build_cases", config.resolve("tools/build_cases.py"))
    bc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bc)
    rng = random.Random(5)
    rows = [{"t": float(i), "obsc": rng.uniform(0, 3), "co": rng.uniform(0, 20), "co2": rng.uniform(0, .3),
             "tc1": rng.uniform(20, 40)} for i in range(60)]
    base = {"obsc": 0.1, "co": 2.0, "co2": 0.04, "tc1": 21.0}
    want = bc.window_features(rows, base)
    got = mem.window_features({"obsc": [r["obsc"] for r in rows], "co": [r["co"] for r in rows],
                               "co2": [r["co2"] for r in rows], "tc": [r["tc1"] for r in rows]},
                              {"obsc": 0.1, "co": 2.0, "co2": 0.04, "tc": 21.0})
    assert all(math.isclose(want[k], got[k], rel_tol=1e-9, abs_tol=1e-12) for k in want)
    assert bc.SCALES == config.load("memory")["scales"] and bc.FEATURES == config.load("memory")["features"]


def test_falling_smoke_never_starts_a_verdict(cfg, library):
    """After a fire is out (or a Twin reset), the smoke falls back: that tail is not 'cooking'."""
    h = Harness(cfg, library)
    h.run(120)
    h.s._last.clear()
    rows = trace("fire")
    h.run(1, "fire", start=len(rows) - 1)             # fill the window with the end of the fire ...
    for _ in range(59):
        h.run(1, "fire", start=len(rows) - 1)
    h.s._last["zone-06"] = ("quiet", h.t)             # ... as if nothing had been said
    before = len(h.cases())
    h.run(30)                                         # smoke drops back to calm
    assert all(c["verdict"] == "quiet" for c in h.cases()[before:])


def test_a_fire_is_never_talked_down_to_cooking(cfg, library):
    h = Harness(cfg, library)
    h.run(120)
    h.run(60, "fire")
    assert h.cases()[-1]["verdict"] == "fire"
    h.run(60, "cooking")                               # smoke without CO right after a fire
    assert "nuisance" not in [c["verdict"] for c in h.cases()]
