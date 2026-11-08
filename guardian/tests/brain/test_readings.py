"""A3-A6 and the Test Book behaviours B1, B2, B3=T5, B7, B9 (synthetic Twin, local mode)."""
import json
from collections import Counter

from src.adapter.envelope import seal, seal_multi


def _reading(s, t="water_level", zone="zone-01", value=0.4, **kw):
    b = s.twin.reading_body(t, zone, value, **kw)
    return s.twin.topic(b), b


def _env(s, b):
    return seal_multi(b, {"brain": "kb", "guardian": s.k_tg})


# ---------------------------------------------------------------- A3 bad HMAC
def test_bad_hmac_verdict_on_topic_sensor_not_retained(bstack):
    s = bstack
    for _ in range(3):
        s.tick()
    retained_before = s.bus.retained["guardian/verdict/power-03"]
    out = s.send_all(s.twin.bad("bad_hmac"))
    assert out == ["bad_hmac"]
    m = s.verdicts("power-03")[-1]
    assert m["topic"] == "guardian/verdict/power-03" and m["verified"]
    assert m["retain"] is False
    b = m["body"]
    assert (b["verdict"], b["reason"], b["sensor_id"]) == ("rejected", "bad_hmac", "power-03")
    assert b["correlation_id"].startswith("tw-r1-")
    assert s.bus.retained["guardian/verdict/power-03"] == retained_before   # real verdict kept
    assert not any("_unattributed" in t for t, *_ in s.bus.raw_history)
    assert "reading_bad_hmac" in s.audit_actions()


def test_bad_hmac_never_quarantines(bstack):
    s = bstack
    for _ in range(10):
        s.send_all(s.twin.bad("bad_hmac"))
    assert s.quarantines() == []
    assert s.run(s.store.is_quarantined, "power-03") is False


def test_bad_hmac_with_invalid_topic_id_sends_nothing_to_brain(bstack):
    s = bstack
    topic, b = _reading(s)
    bad_topic = "city/zone-01/sensor/water_level/Water_01"
    n = len(s.bus.raw_history)
    assert s.send(bad_topic, seal_multi(b, {"guardian": "wrong"})) == "bad_hmac_unattributable"
    assert len(s.bus.raw_history) == n
    assert "reading_bad_hmac" in s.audit_actions()


# ---------------------------------------------------------------- A4 seq
def test_twin_seq_used_and_must_increase(bstack):
    s = bstack
    t, b = _reading(s, seq=500)
    assert s.send(t, _env(s, b)) == "verdict_trusted"
    t, b = _reading(s, seq=499)                         # lower seq, fresh id -> replay
    assert s.send(t, _env(s, b)) == "verdict_rejected"
    assert s.run(s.store.get_verdict, b["id"] + "#replay-1").reason.startswith("replay: non-monotonic seq 499")
    assert s.verdicts("water-01")[-1]["body"]["reason"] == "consistent"   # replay not sent to Brain


def test_missing_seq_falls_back_to_counter(bstack):
    s = bstack
    for _ in range(3):
        b = s.twin.reading_body("water_level", "zone-01", 0.4)
        b.pop("seq")
        assert s.send(s.twin.topic(b), _env(s, b)) == "verdict_trusted"


def test_twin_restart_new_meta_resets_seq(bstack):
    s = bstack
    t, b = _reading(s, seq=900)
    s.send(t, _env(s, b))
    s.send(*s.twin.meta())                              # Twin restarts: new meta id
    t, b = _reading(s, seq=1)
    assert s.send(t, _env(s, b)) == "verdict_trusted"


def test_twin_restart_without_new_meta_is_replay(make_bstack):
    s = make_bstack(seq_reset_on_new_meta=False)
    t, b = _reading(s, seq=900)
    s.send(t, _env(s, b))
    s.send(*s.twin.meta())
    t, b = _reading(s, seq=1)
    assert s.send(t, _env(s, b)) == "verdict_rejected"


def test_duplicate_reading_rejected_not_published_no_quarantine(bstack):
    s = bstack
    s.tick()
    out = s.send_all(s.twin.bad("duplicate"))
    assert out == ["verdict_trusted", "verdict_rejected"]
    assert all(m["body"]["reason"] != "replay" for m in s.verdicts())
    assert s.quarantines() == []


def test_replay_verdicts_can_be_published_unretained(make_bstack):
    s = make_bstack(brain_publish_replay_verdicts=True)
    s.send_all(s.twin.bad("duplicate"))
    m = s.verdicts("traffic-flow-04")[-1]
    assert m["body"]["reason"] == "replay" and m["retain"] is False


# ---------------------------------------------------------------- A5 meta
def test_meta_with_bad_guardian_tag_ignored(make_bstack):
    s = make_bstack(meta=False)
    body = s.twin.meta_body()
    assert s.send("twin/meta", seal_multi(body, {"brain": "kb", "guardian": "wrong"})) == "meta_rejected"
    assert not s.adapter.registry.loaded and "meta_rejected" in s.audit_actions()


def test_meta_without_guardian_tag_accepted_and_logged(make_bstack):
    s = make_bstack(meta=False)
    assert s.send(*s.twin.meta(tagged=False)) == "meta_loaded"
    assert s.adapter.registry.loaded and "meta_loaded_unverified" in s.audit_actions()
    assert len(s.adapter.registry.sensors) == 36 and "valve-01" in s.adapter.registry.actuators


def test_meta_invalid_ignored(make_bstack):
    s = make_bstack(meta=False)
    body = s.twin.meta_body()
    body["sensors"][0].pop("max")
    assert s.send("twin/meta", seal_multi(body, {"guardian": s.k_tg})) == "meta_invalid"


def test_meta_limits_replace_builtin(bstack):
    s = bstack
    lim = s.adapter.registry.limits_for("water-05", "water_level")
    assert (lim.min_value, lim.max_value, lim.unit) == (0.0, 5.0, "m")
    t, b = _reading(s, zone="zone-05", value=7.0)       # inside built-in 0..10, outside meta 0..5
    assert s.send(t, _env(s, b)) == "verdict_rejected"
    assert s.verdicts("water-05")[-1]["body"]["reason"] == "out_of_range"


def test_kit_units_not_rejected_and_conversion(bstack):
    s = bstack
    for _ in range(3):
        assert set(s.tick()) == {"verdict_trusted"}
    # traffic_flow in vehicles/min (built-in table is veh/h): trusted
    assert s.verdicts("traffic-flow-01")[-1]["body"]["verdict"] == "trusted"
    # same sensor reporting in an alias / convertible unit
    t, b = _reading(s, "water_level", "zone-02", 40.0, unit="cm")    # 0.40 m
    assert s.send(t, _env(s, b)) == "verdict_trusted"
    t, b = _reading(s, "traffic_flow", "zone-02", 900.0, unit="veh/h")  # 15 vehicles/min
    assert s.send(t, _env(s, b)) == "verdict_trusted"
    t, b = _reading(s, "power_load", "zone-02", 800.0, unit="m")       # not convertible
    assert s.send(t, _env(s, b)) == "verdict_rejected"
    assert s.verdicts("power-02")[-1]["body"]["reason"] == "unit_mismatch"


def test_unknown_type_uses_meta_min_max_only(make_bstack):
    s = make_bstack(meta=False)
    body = s.twin.meta_body()
    body["sensors"].append({"sensor_id": "noise-01", "zone_id": "zone-01", "type": "noise",
                            "unit": "dB", "min": 0, "max": 120})
    s.send("twin/meta", seal_multi(body, {"guardian": s.k_tg}))
    for v in (40, 110, 30):                               # big steps: no rate rule for unknown type
        b = {"id": s.twin._id(), "timestamp": s.twin.reading_body("water_level", "zone-01", 0)["timestamp"],
             "source": "twin", "sensor_id": "noise-01", "zone_id": "zone-01", "type": "noise",
             "value": v, "unit": "dB", "seq": s.twin.n}
        assert s.send("city/zone-01/sensor/noise/noise-01", _env(s, b)) == "verdict_trusted"
    b["id"], b["value"], b["seq"] = s.twin._id(), 130, b["seq"] + 1
    assert s.send("city/zone-01/sensor/noise/noise-01", _env(s, b)) == "verdict_rejected"


def test_unknown_sensor_audit_only(bstack):
    s = bstack
    n = len(s.bus.raw_history)
    assert s.send_all(s.twin.bad("unknown_id")) == ["unknown_id"]
    b = s.twin.reading_body("water_level", "zone-06", 0.4)
    b["sensor_id"] = "water-99"
    assert s.send("city/zone-06/sensor/water_level/water-99", _env(s, b)) == "unknown_id"
    assert len(s.bus.raw_history) == n
    assert "reading_unknown_sensor" in s.audit_actions()


# ---------------------------------------------------------------- A6 calls / topics
def test_calls_and_other_topics_never_judged(bstack):
    s = bstack
    n = len(s.bus.raw_history)
    call = {"id": "tw-r1-call", "timestamp": "2026-11-14T10:00:00.000Z", "source": "twin",
            "call_id": "call-1", "zone_id": "zone-03", "category": "fire", "text": "fire!"}
    for _ in range(10):                                   # B7: ten fake fire calls
        assert s.send("city/zone-03/call", _env(s, call)) == "ignored"
    assert s.send("twin/status", seal({"status": "online"}, "k")) == "ignored"
    assert s.send("city/zone-03/sensor/water_level", "{}") == "ignored"   # wrong depth
    assert len(s.bus.raw_history) == n
    assert "city/zone-03/call" not in " ".join(s.adapter.subscriptions())


# ---------------------------------------------------------------- B9
def test_b9_bad_messages_never_crash_and_are_accounted(bstack):
    s = bstack
    for _ in range(3):
        s.tick()
    audit0 = len(s.audit_actions())
    results = {}
    for kind in ("malformed", "bad_hmac", "duplicate", "unknown_id", "impossible", "old"):
        results[kind] = s.send_all(s.twin.bad(kind))
    assert results["malformed"] == ["malformed"]
    assert results["bad_hmac"] == ["bad_hmac"]
    assert results["duplicate"][-1] == "verdict_rejected"
    assert results["unknown_id"] == ["unknown_id"]
    assert results["impossible"] == ["verdict_rejected"]
    assert results["old"] == ["stale"]
    v = s.verdicts("water-05")[-1]["body"]
    assert (v["verdict"], v["reason"]) == ("rejected", "out_of_range")
    acts = s.audit_actions()[audit0:]
    for a in ("reading_malformed", "reading_bad_hmac", "verdict_rejected", "reading_unknown_sensor",
              "reading_stale"):
        assert a in acts
    assert s.quarantines() == []
    assert s.send("city/zone-01/sensor/water_level/water-01", b"\xff\xfe") == "malformed"
    assert s.send("twin/meta", "garbage") == "meta_malformed"
    assert s.send("brain/alert", "{}") == "alert_unverified"


# ---------------------------------------------------------------- B1 / B2 / B3=T5 / fire
def test_b1_calm_day_rush_hour_every_sensor_trusted(bstack):
    s = bstack
    for _ in range(60):
        s.tick("rush")
    c = Counter(m["body"]["verdict"] for m in s.verdicts())
    assert c == {"trusted": 60 * 36}
    peak = [m["body"] for m in s.verdicts("traffic-density-03")]
    assert len(peak) == 60 and s.quarantines() == []


def test_b1_slow_changes_every_type(bstack):
    """Every real Twin type drifting slowly (10% of its range over 60 s) stays trusted."""
    s = bstack
    from mocks.brain_twin_sim import TYPES
    for i in range(60):
        s.twin.t += 1
        for t, (_, unit, lo, hi, *_r) in TYPES.items():
            v = lo + 0.2 * (hi - lo) + 0.1 * (hi - lo) * i / 60
            b = s.twin.reading_body(t, "zone-02", v)
            assert s.send(s.twin.topic(b), _env(s, b)) == "verdict_trusted", (t, i)


def test_b2_real_flood_water01_trusted_all_along(bstack):
    s = bstack
    for _ in range(80):
        s.tick("flood")
    w = [m["body"] for m in s.verdicts("water-01")]
    assert len(w) == 80 and all(b["verdict"] == "trusted" for b in w)
    assert max(s.twin.water01, 0) >= 0.4


def test_b3_t5_fake_flood_rejected_and_quarantined_in_time(bstack):
    s = bstack
    first_reject = quarantined_at = None
    for _ in range(45):
        s.tick("calm", falsify=(10, 40))
        if first_reject is None and any(m["body"]["verdict"] == "rejected" for m in s.verdicts("water-01")):
            first_reject = s.twin.t
        if quarantined_at is None and s.quarantines():
            quarantined_at = s.twin.t
    v = [m["body"] for m in s.verdicts("water-01") if m["body"]["verdict"] == "rejected"][0]
    assert v["reason"] == "implausible_jump" and v["trust_score"] < 0.4
    assert first_reject - 10 < 10                    # < 10 s after the fake starts
    assert quarantined_at <= 20                       # within 20 s of the run start
    q = s.quarantines()[0]
    assert q["body"]["device_id"] == "water-01" and q["body"]["state"] == "quarantined"
    assert q["body"]["reason"] == "implausible_jump" and q["retain"] is True
    # verdict published before the quarantine notice it caused
    topics = [t for t, *_ in s.bus.raw_history if t.startswith("guardian/")]
    qi = topics.index("guardian/quarantine/water-01")
    assert topics[qi - 1] == "guardian/verdict/water-01"
    # other sensors unaffected
    assert all(m["body"]["verdict"] == "trusted" for m in s.verdicts() if m["body"]["sensor_id"] != "water-01")


def test_one_glitch_does_not_quarantine(bstack):
    s = bstack
    for i in range(30):
        s.tick("calm", falsify=(10, 11) if i < 15 else (20, 21))
    assert s.quarantines() == []
    assert Counter(m["body"]["verdict"] for m in s.verdicts("water-01"))["rejected"] == 2


def test_fire_steps_never_rejected(bstack):
    s = bstack
    for _ in range(40):
        s.tick("fire")
    assert not [m for m in s.verdicts() if m["body"]["verdict"] == "rejected"]
