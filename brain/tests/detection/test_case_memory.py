"""Detection with the case memory (brain/internal/case): cooking held down, a fire confirmed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.common import config

CASE = config.load("topics")["internal"]["case"]["topic"]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def case(city, zone: str, verdict: str, share: float = 1.0, k: int = 15) -> None:
    n = round(share * k)
    msg = {"id": "mem-1", "timestamp": iso(city.clock.t), "correlation_id": "twin-x", "zone_id": zone,
           "incident_type": "fire", "verdict": verdict, "share": share, "k": k, "votes": {verdict: n},
           "sources": {}, "features": {}, "sensors": [], "library": "NIST",
           "summary": f"{zone}: {verdict} ({n} of {k} similar lab cases)"}
    city.s.handle(CASE, json.dumps(msg))


def smoke(city, zone: str = "zone-06") -> None:
    city.overrides[(zone, "air_quality")] = 250.0      # above alert (150): as much smoke as the B5 fire


def test_detection_listens_to_the_case_memory(make_city):
    assert (CASE, 1) in make_city().s.subscriptions()


def test_cooking_verdict_keeps_smoke_alone_from_opening_a_fire(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    city.step(8)
    assert bus.incidents(type="fire") == []          # held: waiting for the case memory
    case(city, "zone-06", "nuisance")
    for _ in range(6):
        city.step(5)
        case(city, "zone-06", "nuisance")             # repeated while it lasts (memory.repeat_s)
    assert bus.incidents(type="fire") == []


def test_without_a_verdict_the_hold_ends(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    wait = det_cfg["incidents"]["fire"]["memory"]["wait_s"]
    city.step(int(wait) - 2)
    assert bus.incidents(type="fire") == []
    city.step(6)
    assert bus.incidents(status="opened", type="fire", zone_id="zone-06")


def test_a_call_ends_the_hold_at_once(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    city.call("zone-06", "fire", "the kitchen is on fire")
    city.step(5)
    assert bus.incidents(status="opened", type="fire", zone_id="zone-06")


def test_cooking_verdict_does_not_silence_residents(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    case(city, "zone-06", "nuisance")
    for _ in range(3):
        city.call("zone-06", "fire", "flames in the flat")
    city.step(4)
    opened = bus.incidents(status="opened", type="fire", zone_id="zone-06")
    assert opened and "nuisance" in (opened[0].get("case") or "")   # the memory's line travels with it


def test_fire_verdict_is_support_not_evidence(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    city.step(3)
    case(city, "zone-06", "fire", 14 / 15)
    city.step(4)
    inc = bus.incidents(status="opened", type="fire", zone_id="zone-06")[0]
    assert "fire" in inc["case"]
    assert all(e["kind"] in ("reading", "call") for e in inc["evidence"])      # never a source for S3 / S6
    assert "case memory" in inc["reason"]


def test_weak_or_stale_verdicts_are_ignored(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city)
    case(city, "zone-06", "nuisance", share=0.4)       # below min_share: no say
    city.step(int(det_cfg["incidents"]["fire"]["memory"]["wait_s"]) + 4)
    assert bus.incidents(status="opened", type="fire", zone_id="zone-06")


def test_zone_without_memory_sensors_is_not_held(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(30)
    smoke(city, "zone-03")                             # Centre: no smoke / CO sensor
    city.step(5)
    assert bus.incidents(status="opened", type="fire", zone_id="zone-03")
