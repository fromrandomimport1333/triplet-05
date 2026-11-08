"""Rule fallback: every incident type resolved to real devices of the mock city."""

from __future__ import annotations

import pytest

from tests.scoring.helpers import incident, send, unit_ack

Q = "brain/internal/quarantine/"


def rule_actions(bus, iid):
    p = bus.proposals("rules", iid)[-1]
    return [(a["target_id"], a["action"], a["params"]) for a in p["actions"]]


@pytest.mark.parametrize("type_, zone, severity, expected", [
    ("flood", "zone-01", "medium", [("valve-01", "open", {"opening": 1.0}), ("signal-01", "priority", {})]),
    ("flood", "zone-02", "high", [("valve-02", "open", {"opening": 1.0}), ("signal-02", "priority", {}),
                                  ("unit-rescue-01", "dispatch", {"zone_id": "zone-02"})]),
    ("fire", "zone-05", "medium", [("unit-fire-01", "dispatch", {"zone_id": "zone-05"}), ("signal-05", "priority", {})]),
    ("fire", "zone-01", "medium", [("unit-fire-02", "dispatch", {"zone_id": "zone-01"}), ("signal-01", "priority", {})]),
    ("accident", "zone-03", "medium", [("unit-ambulance-02", "dispatch", {"zone_id": "zone-03"}),
                                       ("signal-03", "priority", {}), ("signal-07", "priority", {})]),
    ("accident", "zone-04", "high", [("unit-ambulance-01", "dispatch", {"zone_id": "zone-04"}),
                                     ("signal-04", "priority", {}),
                                     ("unit-police-01", "dispatch", {"zone_id": "zone-04"})]),
    ("traffic_jam", "zone-03", "low", [("signal-03", "priority", {}), ("signal-07", "priority", {})]),
    ("power_problem", "zone-06", "medium", [("grid-01", "shed", {})]),
    ("power_problem", "zone-04", "medium", [("grid-02", "shed", {})]),
])
def test_rule_proposal_per_type(svc, bus, type_, zone, severity, expected):
    send(svc, "brain/internal/incident", incident("inc-1", type_, zone, severity=severity))
    assert rule_actions(bus, "inc-1") == expected
    p = bus.proposals("rules", "inc-1")[-1]
    assert p["id"].startswith("rules-") and p["correlation_id"] == "twin-inc-1"
    assert bus.events("rules_proposal")


def test_quarantined_valve_never_used(svc, bus):
    send(svc, Q + "valve-01", {"id": "in-1", "timestamp": "t", "device_id": "valve-01",
                               "state": "quarantined", "reason": "inconsistent"}, True)
    send(svc, "brain/internal/incident", incident("inc-1", "flood", "zone-01"))
    acts = rule_actions(bus, "inc-1")
    assert acts == [("signal-01", "priority", {})]
    assert "no usable valve" in bus.proposals("rules", "inc-1")[-1]["justification"]


def test_busy_unit_replaced_by_another(svc, bus):
    send(svc, "brain/internal/ack", unit_ack("unit-fire-01", "on_scene", "zone-06"))
    send(svc, "brain/internal/incident", incident("inc-1", "fire", "zone-05"))
    assert rule_actions(bus, "inc-1")[0] == ("unit-fire-02", "dispatch", {"zone_id": "zone-05"})


def test_no_free_unit_rest_still_proposed(svc, bus):
    for u in ("unit-fire-01", "unit-fire-02"):
        send(svc, "brain/internal/ack", unit_ack(u, "on_scene"))
    send(svc, "brain/internal/incident", incident("inc-1", "fire", "zone-05"))
    p = bus.proposals("rules", "inc-1")[-1]
    assert [(a["target_id"], a["action"]) for a in p["actions"]] == [("signal-05", "priority")]
    assert "no free fire_truck" in p["justification"]


def test_quarantined_unit_skipped(svc, bus):
    send(svc, Q + "unit-ambulance-02", {"id": "in-1", "timestamp": "t", "device_id": "unit-ambulance-02",
                                        "state": "quarantined", "reason": "x"}, True)
    send(svc, "brain/internal/incident", incident("inc-1", "accident", "zone-03"))
    assert rule_actions(bus, "inc-1")[0][0] == "unit-ambulance-01"


def test_no_meta_no_proposal(make_service, bus):
    svc = make_service(meta=False)
    send(svc, "brain/internal/incident", incident("inc-1", "flood", "zone-01", severity="critical"))
    assert bus.proposals("rules") == []
    assert "no city description" in bus.events("decision_skipped")[0]["summary"]
