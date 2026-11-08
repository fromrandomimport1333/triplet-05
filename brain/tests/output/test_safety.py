"""Each safety rule refuses its case and lets the valid case through (team plan 05, section 8)."""

from __future__ import annotations

from .conftest import WATER2


def refused(h) -> list[dict]:
    return h.bus.outcomes("refused")


def sent(h) -> list[dict]:
    return h.bus.outcomes("sent")


# --- S1 quarantine --------------------------------------------------------------------

def test_s1_quarantined_target_refused(h):
    h.quarantine("valve-01")
    h.incident(WATER2 + [{"source_id": "air-01", "kind": "reading", "trust": 1.0}])
    h.decision(("valve-01", "open", {"opening": 0.8}))
    [o] = refused(h)
    assert o["rule"] == "S1" and "quarantined" in o["detail"]
    [e] = h.bus.events("action_refused")
    assert "S1" in e["summary"] and e["correlation_id"] == "twin-000182" and e["incident_id"] == "inc-0001"
    assert h.commands() == []


def test_s1_released_target_passes(h):
    h.quarantine("valve-01")
    h.quarantine("valve-01", "released")
    h.incident(WATER2)
    h.decision(("valve-01", "open", {}))
    assert len(sent(h)) == 1 and refused(h) == []


# --- S2 rejected evidence -------------------------------------------------------------

def test_s2_all_evidence_rejected_refused(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 0.0},
                {"source_id": "water-02", "kind": "reading", "trust": 0.0}])
    h.decision(("signal-01", "priority", {}))
    [o] = refused(h)
    assert o["rule"] == "S2"


def test_s2_current_rejected_verdict_overrides_evidence_trust(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0}])
    h.trust("water-01", "rejected", 0.05)
    h.decision(("signal-01", "priority", {}))
    assert refused(h)[0]["rule"] == "S2"


def test_s2_one_trusted_piece_passes(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 0.0},
                {"source_id": "water-02", "kind": "reading", "trust": 0.9}])
    h.decision(("signal-01", "priority", {}))
    assert len(sent(h)) == 1


# --- S3 two sources for risky actions ---------------------------------------------------

def test_s3_one_sensor_only_refused(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0}])
    h.decision(("valve-01", "open", {}))
    [o] = refused(h)
    assert o["rule"] == "S3" and "has 1" in o["detail"]


def test_s3_many_calls_count_as_one_source(h):
    calls = [{"source_id": f"call-{i}", "kind": "call", "category": "fire", "trust": 1.0} for i in range(10)]
    h.incident(calls, type_="fire")
    h.decision(("unit-fire-01", "dispatch", {}))
    [o] = refused(h)
    assert o["rule"] == "S3" and "calls" in o["detail"]


def test_s3_sensor_plus_calls_passes(h):
    h.incident(WATER2)
    h.decision(("valve-01", "open", {}))
    assert len(sent(h)) == 1


def test_s3_two_sensors_pass_and_rejected_one_does_not_count(h):
    ev = [{"source_id": "water-01", "kind": "reading", "trust": 1.0},
          {"source_id": "water-02", "kind": "reading", "trust": 1.0}]
    h.incident(ev)
    h.decision(("valve-01", "open", {}))
    assert len(sent(h)) == 1
    h.trust("water-02", "rejected", 0.0)
    h.decision(("grid-01", "shed", {}))
    assert refused(h)[0]["rule"] == "S3"


def test_s3_uses_decision_evidence_sensors_without_incident(h):
    h.decision(("valve-01", "open", {}), incident_id="inc-unknown", evidence_sensors=["water-01", "water-02"])
    assert len(sent(h)) == 1


def test_s3_does_not_apply_to_safe_actions(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0}])
    h.decision(("signal-01", "priority", {}), ("valve-01", "close", {}))
    assert len(sent(h)) == 2


# --- S4 cooldown ------------------------------------------------------------------------

def test_s4_same_action_within_cooldown_refused_then_allowed(h):
    h.incident(WATER2)
    h.decision(("signal-01", "priority", {}))
    h.clock.t += 10
    h.decision(("signal-01", "priority", {}))
    [o] = refused(h)
    assert o["rule"] == "S4" and "10 s ago" in o["detail"]
    h.clock.t += 51
    h.decision(("signal-01", "priority", {}))
    assert len(sent(h)) == 2


def test_s4_other_action_same_device_passes(h):
    h.incident(WATER2)
    h.decision(("signal-01", "priority", {}))
    h.decision(("signal-01", "normal", {}))
    assert len(sent(h)) == 2


def test_s4_any_action_scope(make, safety_cfg):
    safety_cfg["s4_scope"] = "any_action"
    h = make(safety=safety_cfg)
    h.incident(WATER2)
    h.decision(("signal-01", "priority", {}))
    h.decision(("signal-01", "normal", {}))
    assert refused(h)[0]["rule"] == "S4"


# --- S5 allowed action ----------------------------------------------------------------

def test_s5_action_not_allowed_refused(h):
    h.incident(WATER2)
    h.decision(("signal-01", "open", {}))
    [o] = refused(h)
    assert o["rule"] == "S5" and "not allowed" in o["detail"]


def test_s5_unit_actions(h):
    h.incident(WATER2)
    h.decision(("unit-fire-01", "open", {}))
    assert refused(h)[0]["rule"] == "S5"
    h.decision(("unit-fire-01", "recall", {}))
    assert len(sent(h)) == 1


def test_s5_unknown_target_refused(h):
    h.incident(WATER2)
    h.decision(("valve-99", "open", {}))
    [o] = refused(h)
    assert o["rule"] == "S5" and "unknown target" in o["detail"]


def test_no_city_description_refuses(make):
    h = make(with_meta=False)
    h.decision(("valve-01", "open", {}))
    assert "no city description" in refused(h)[0]["detail"]


# --- S6 Guardian offline ----------------------------------------------------------------

def test_s6_guardian_offline_same_type_sensors_refused(h):
    h.layer("guardian", "offline")
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0},
                {"source_id": "water-02", "kind": "reading", "trust": 1.0}])
    h.decision(("valve-01", "open", {}))
    [o] = refused(h)
    assert o["rule"] == "S6"


def test_s6_sensor_plus_calls_not_enough_offline(h):
    h.layer("guardian", "offline")
    h.incident(WATER2)
    h.decision(("valve-01", "open", {}))
    assert refused(h)[0]["rule"] == "S6"


def test_s6_two_sensor_types_pass_offline(h):
    h.layer("guardian", "offline")
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0},
                {"source_id": "air-01", "kind": "reading", "trust": 1.0}])
    h.decision(("valve-01", "open", {}))
    assert len(sent(h)) == 1


def test_s6_safe_mode_decision_counts_as_offline(h):
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0},
                {"source_id": "water-02", "kind": "reading", "trust": 1.0}])
    h.decision(("valve-01", "open", {}), mode="safe")
    assert refused(h)[0]["rule"] == "S6"


def test_s6_non_risky_action_passes_offline(h):
    h.layer("guardian", "offline")
    h.incident([{"source_id": "water-01", "kind": "reading", "trust": 1.0}])
    h.decision(("signal-01", "priority", {}))
    assert len(sent(h)) == 1


def test_rules_run_in_order(h):
    """Quarantined AND not allowed: S1 is reported."""
    h.quarantine("signal-01")
    h.incident(WATER2)
    h.decision(("signal-01", "open", {}))
    assert refused(h)[0]["rule"] == "S1"


def test_safe_mode_events(h):
    h.layer("guardian", "online")
    assert h.bus.events("safe_mode_on") == [] and h.bus.events("safe_mode_off") == []
    h.layer("guardian", "offline")
    h.layer("guardian", "offline")
    assert len(h.bus.events("safe_mode_on")) == 1
    h.layer("guardian", "online")
    assert len(h.bus.events("safe_mode_off")) == 1


def test_s7_no_command_while_the_twin_is_offline(h):
    h.incident(WATER2)
    h.layer("twin", "offline")
    h.decision(("signal-01", "priority", {}))
    [o] = refused(h)
    assert o["rule"] == "S7" and "Twin offline" in o["detail"]
    assert sent(h) == []
    h.layer("twin", "online")
    h.decision(("signal-01", "priority", {}))
    assert len(sent(h)) == 1


def test_twin_restart_ends_the_cooldowns(h):
    h.incident(WATER2)
    h.layer("twin", "online")
    h.decision(("signal-01", "priority", {}))
    h.clock.t += 10
    h.layer("twin", "offline")
    h.layer("twin", "online")                      # restart: actuators back to their defaults
    h.decision(("signal-01", "priority", {}))
    assert len(sent(h)) == 2 and not [o for o in refused(h) if o["rule"] == "S4"]
