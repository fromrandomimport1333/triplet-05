"""Detection: per-sensor anomalies, combination, Guardian trust, lifecycle (02-graph section 4)."""

from __future__ import annotations

import json
import math

from src.common.envelope import parse_envelope, verify_hmac
from src.common.models import Incident
from src.detection.sensors import SensorState

WARMUP = 30


def flood(start: int, peak: float = 2.5):
    """Twin mock flood: jumps to 1.4 m and rises 0.15 m per tick up to `peak`."""
    return lambda n: 0.4 if n < start else min(peak, 1.4 + (n - start) * 0.15)


def test_calm_city_with_noise_has_no_incident(make_city, bus):
    city = make_city(seed=3)
    city.trust_all()
    city.step(600)
    assert bus.incidents() == []


def test_calm_city_with_more_noise_still_quiet(make_city, bus):
    city = make_city(seed=5)
    city.noise = 0.05
    city.trust_all()
    city.step(400)
    assert bus.incidents() == []


def test_real_flood_opens_fast_updates_and_closes(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    start = city.n + 1
    city.overrides[("zone-01", "water_level")] = flood(start)
    city.step(5)
    opened = bus.incidents(status="opened")
    assert len(opened) == 1, "opened within 3 to 5 readings"
    inc = opened[0]
    Incident.model_validate(inc)
    assert inc["type"] == "flood" and inc["zone_id"] == "zone-01"
    assert inc["verified"] is True and inc["confidence"] >= 0.5
    assert inc["correlation_id"] == f"twin-{start}-water-01"     # the first bad reading started the chain
    assert inc["incident_id"] == "inc-0001"
    assert any(e["source_id"] == "water-01" and e["kind"] == "reading" for e in inc["evidence"])
    assert "water-01 at alert level" in inc["reason"]
    city.step(20)
    updated = bus.incidents(status="updated")
    assert updated and updated[-1]["severity"] in ("high", "critical")
    city.overrides.pop(("zone-01", "water_level"))
    city.step(10)
    closed = bus.incidents(status="closed")
    assert len(closed) == 1 and closed[0]["incident_id"] == "inc-0001"
    names = [e["event"] for e in bus.events()]
    assert names[0] == "incident_opened" and "incident_closed" in names
    assert bus.events("incident_opened")[0]["summary"].startswith("flood opened at zone-01 Port (confidence")
    # one incident for the whole flood, every message for the same chain
    assert {i["incident_id"] for i in bus.incidents()} == {"inc-0001"}
    assert {i["correlation_id"] for i in bus.incidents()} == {inc["correlation_id"]}


def test_long_flood_no_duplicates_and_updates_rate_limited(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(600)                                   # ten minutes at the plateau
    incs = bus.incidents()
    assert len(bus.incidents(status="opened")) == 1
    assert len(incs) <= 4, [i["status"] for i in incs]
    assert not bus.incidents(status="closed")
    times = [i["timestamp"] for i in incs]
    assert times == sorted(times)


def test_flood_with_flood_calls_adds_evidence(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(5)
    for _ in range(3):
        city.call("zone-01", "flood", "water in the street")
        city.step(3)
    city.step(5)
    up = bus.incidents(status="updated")
    assert any(any(e["kind"] == "call" for e in u["evidence"]) for u in up)
    assert up[-1]["confidence"] > bus.incidents(status="opened")[0]["confidence"]


def test_fake_flood_with_rejected_sensor_gives_nothing(make_city, bus):
    city = make_city()
    city.trust_all()
    city.trust("water-01", "rejected", 0.1)
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(60)
    assert bus.incidents() == []
    ev = bus.events("evidence_rejected")
    assert len(ev) == 1 and "water-01" in ev[0]["summary"] and "rejected by Guardian" in ev[0]["summary"]


def test_suspicious_sensor_alone_does_not_open(make_city, bus):
    city = make_city()
    city.trust_all()
    city.trust("water-01", "suspicious")
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(30)
    assert bus.incidents() == []


def test_guardian_offline_incident_not_verified(make_city, bus):
    city = make_city()
    city.trust_all()
    city.guardian("offline")
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(6)
    opened = bus.incidents(status="opened")
    assert len(opened) == 1
    assert opened[0]["verified"] is False
    assert opened[0]["evidence"][0]["trust"] == 0.6
    assert "not verified by Guardian" in bus.events("incident_opened")[0]["summary"]


def test_no_verdict_yet_counts_as_unverified(make_city, bus):
    city = make_city()                               # no trust message at all
    city.step(WARMUP)
    city.overrides[("zone-02", "water_level")] = flood(city.n + 1)
    city.step(6)
    opened = bus.incidents(status="opened")
    assert len(opened) == 1 and opened[0]["zone_id"] == "zone-02" and opened[0]["verified"] is False


def test_rush_hour_city_wide_is_not_a_jam(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    start, end = city.n + 1, city.n + 241

    def rush(n, zone, stype):
        f = math.sin(math.pi * (n - start) / (end - start)) if start <= n <= end else 0.0
        return {"traffic_speed": 1 - 0.4 * f, "traffic_density": 1 + 2.0 * f, "traffic_flow": 1 + 0.8 * f}.get(stype, 1.0)

    city.factors = rush
    city.step(300)
    assert bus.incidents() == []


def test_local_jam_opens_traffic_jam(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-03", "traffic_speed")] = 7.0
    city.overrides[("zone-03", "traffic_density")] = 180.0
    city.overrides[("zone-03", "traffic_flow")] = 4.0
    min_s = det_cfg["incidents"]["traffic_jam"]["sensors"]["traffic_speed"]["min_abnormal_s"]
    city.step(min_s - 5)
    assert bus.incidents() == [], "a jam needs the speed low for min_abnormal_s"
    city.step(15)
    opened = bus.incidents(status="opened")
    assert [i["type"] for i in opened] == ["traffic_jam"], "no accident without a call"
    assert opened[0]["zone_id"] == "zone-03"


def test_local_jam_during_rush_hour_still_found(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.factors = lambda n, zone, stype: {"traffic_speed": 0.6, "traffic_density": 3.0,
                                           "traffic_flow": 1.8}.get(stype, 1.0)
    city.overrides[("zone-04", "traffic_speed")] = 7.0
    city.step(60)
    opened = bus.incidents(status="opened")
    assert [(i["type"], i["zone_id"]) for i in opened] == [("traffic_jam", "zone-04")]


def test_accident_from_calls_and_speed_drop(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-06", "traffic_speed")] = 5.0
    city.overrides[("zone-06", "traffic_density")] = 160.0
    city.overrides[("zone-06", "traffic_flow")] = 2.0
    for _ in range(40):
        city.call("zone-06", "accident", "two cars crashed")
        city.step()
    opened = bus.incidents(status="opened")
    assert [i["type"] for i in opened] == ["accident"], "the queue behind an accident is not a separate jam"
    kinds = {e["kind"] for e in bus.incidents(zone_id="zone-06")[-1]["evidence"]}
    assert kinds == {"reading", "call"}


def test_fire_from_air_and_calls_suppresses_power_problem(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-05", "air_quality")] = 250.0
    city.overrides[("zone-05", "power_load")] = 650.0
    for _ in range(20):
        city.call("zone-05", "fire", "smoke at the substation")
        city.step()
    opened = bus.incidents(status="opened")
    assert [i["type"] for i in opened] == ["fire"]
    last = bus.incidents(type="fire")[-1]
    assert last["confidence"] >= 0.9 and last["severity"] in ("high", "critical")
    assert not bus.incidents(type="power_problem")


def test_power_problem_without_fire(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-04", "power_load")] = 900.0
    city.step(10)
    opened = bus.incidents(status="opened")
    assert [(i["type"], i["zone_id"]) for i in opened] == [("power_problem", "zone-04")]
    assert opened[0]["confidence"] <= 0.6, "one z-only source stays under cap_single"


def test_ten_fire_calls_alone_give_one_low_incident(make_city, bus, det_cfg):
    """B7: ten fake fire calls are one source: at most one low-confidence, unverified fire."""
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    for _ in range(10):
        city.call("zone-02", "fire", "fire!")
    city.step(30)
    incs = bus.incidents()
    assert len(bus.incidents(status="opened")) == 1
    assert all(i["confidence"] <= det_cfg["calls"]["cap"] for i in incs)
    assert all(i["severity"] == "low" and i["verified"] is False for i in incs)
    assert len(incs[0]["evidence"]) <= det_cfg["calls"]["max_evidence"]
    city.step(det_cfg["calls"]["window_s"])
    assert len(bus.incidents(status="closed")) == 1, "closed when the calls age out"


def test_closes_when_sensor_goes_silent(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(8)
    assert bus.incidents(status="opened")
    city.silent.add("water-01")
    city.step(int(det_cfg["sensor"]["stale_s"]) + 3)
    closed = bus.incidents(status="closed")
    assert len(closed) == 1 and "no live evidence" in closed[0]["reason"]


def test_spike_is_noise(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-03", "air_quality")] = lambda n: 400.0 if n == WARMUP + 2 else 12.0
    city.step(10)
    city.overrides[("zone-03", "air_quality")] = lambda n: 400.0 if n % 2 else 12.0   # never 3 in a row
    city.step(20)
    assert bus.incidents() == []


def test_neighbour_water_supports_but_never_opens_alone(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(10)
    assert [i["zone_id"] for i in bus.incidents(status="opened")] == ["zone-01"]


def test_timeline_events_are_sealed_when_keys_given(make_city, bus):
    keys = {"twin": b"k1", "guardian": b"k2"}
    city = make_city(seal_keys=keys)
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(6)
    raw = [p for t, p, _, _ in bus.sent if t == "brain/event"]
    assert raw
    env = parse_envelope(raw[0])
    verify_hmac(env, b"k2", "guardian")
    assert json.loads(env.body)["event"] == "incident_opened"


def test_internal_messages_are_plain_json(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(6)
    raw = [p for t, p, _, _ in bus.sent if t == "brain/internal/incident"]
    assert "body" not in json.loads(raw[0])


def test_bad_messages_are_ignored(make_city, bus):
    city = make_city()
    city.s.handle("brain/internal/reading", b"not json")
    city.s.handle("brain/internal/reading", json.dumps({"sensor_id": "x"}))
    city.s.handle("brain/internal/call", json.dumps([1, 2]))
    city.step(3)
    assert bus.incidents() == []


def test_sensor_baseline_frozen_while_abnormal(det_cfg):
    cfg = det_cfg["sensor"]
    st = SensorState("water-01", "zone-01", "water_level", "m", 1.3, "up")
    for i in range(40):
        st.update(0.4 + 0.004 * (i % 3 - 1), float(i), f"c{i}", cfg)
    mean = st.mean
    for i in range(40, 300):
        st.update(2.0, float(i), f"c{i}", cfg)
    assert st.abnormal and abs(st.mean - mean) < 1e-9
    assert st.onset_corr == "c40"
    for i in range(300, 305):
        st.update(0.4, float(i), f"c{i}", cfg)
    assert not st.abnormal


def test_old_calls_do_not_keep_a_flood_open_after_the_sensor_recovers(make_city, bus):
    """Scenario B2: residents' calls made during the flood must not keep it open for their whole
    120 s window once the trusted water sensor says the water is back to normal."""
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(5)
    for _ in range(3):
        city.call("zone-01", "flood", "water in the street")
        city.step(3)
    assert bus.incidents(status="opened")
    city.overrides.pop(("zone-01", "water_level"))
    city.step(10)                                    # far less than the 120 s call window
    assert bus.incidents(status="closed"), "closed once the sensor is back to normal"


def test_a_new_call_after_recovery_still_counts(make_city, bus, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    city.overrides[("zone-01", "water_level")] = flood(city.n + 1)
    city.step(10)
    city.overrides.pop(("zone-01", "water_level"))
    city.step(10)
    assert bus.incidents(status="closed")
    for _ in range(3):                               # residents report water again, sensor says normal
        city.call("zone-01", "flood", "water coming back")
        city.step(2)
    calls_only = [i for i in bus.incidents(status="opened")]
    assert len(calls_only) <= 2                      # a new, calls-only report is still heard (capped)


def _twin(city, status):
    msg = {"id": "in-ls", "timestamp": "2026-11-14T10:00:00.000Z", "layer": "twin", "status": status}
    city.s.handle("brain/internal/layer-status/twin", json.dumps(msg), True)


def test_twin_restart_closes_incidents_and_forgets_calls(make_city, bus):
    """A scenario reset (Twin offline -> online) must not leave the last run's calls or incidents behind."""
    city = make_city()
    city.trust_all()
    _twin(city, "online")
    city.step(WARMUP)
    for _ in range(3):
        city.call("zone-05", "fire", "smoke")
        city.step(2)
    assert bus.incidents(status="opened", type="fire")
    _twin(city, "offline")
    _twin(city, "online")
    closed = bus.incidents(status="closed", type="fire")
    assert closed and closed[-1]["reason"] == "Twin restarted: its city state was reset"
    city.step(10)
    assert len(bus.incidents(status="opened", type="fire")) == 1        # the old calls do not reopen it


def test_first_twin_online_is_not_a_restart(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    for _ in range(3):
        city.call("zone-05", "fire", "smoke")
        city.step(2)
    _twin(city, "online")                       # first status seen: nothing to reset
    assert not bus.incidents(status="closed")


def test_a_redelivered_call_counts_once(make_city, bus):
    city = make_city()
    city.trust_all()
    city.step(WARMUP)
    msg = {"id": "in-c1", "timestamp": "2026-11-14T10:00:00.000Z", "correlation_id": "twin-c1",
           "call_id": "call-dup", "zone_id": "zone-05", "category": "fire", "text": "smoke"}
    for _ in range(3):                                   # QoS 1 redelivery of the same call
        city.s.handle("brain/internal/call", json.dumps(msg))
    assert len([c for c in city.s.calls if c.call_id == "call-dup"]) == 1
