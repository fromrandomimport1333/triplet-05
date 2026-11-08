"""Scoring service: when to decide, which proposal wins, units, safe mode, re-decisions."""

from __future__ import annotations

import pytest

from src.common.envelope import body_text, seal_multi
from src.common.models import Proposal
from tests.scoring.helpers import T0, actions_of, call, incident, outcome, proposal, reading, send, unit_ack

INC = "brain/internal/incident"
PROP = "brain/internal/proposal"
LLM_STATUS = "brain/internal/llm-status"
OUT = "brain/internal/outcome"
ACK = "brain/internal/ack"


def offline(svc):
    send(svc, LLM_STATUS, {"id": "llm-1", "timestamp": "t", "status": "offline"}, True)


def guardian(svc, status):
    send(svc, "brain/internal/layer-status/guardian",
         {"id": "in-1", "timestamp": "t", "layer": "guardian", "status": status}, True)


# --- when to decide ---------------------------------------------------------------------


def test_waits_for_llm_then_better_llm_wins(svc, bus, clock):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    assert bus.decisions() == []                           # waiting for the LLM
    clock.t += 3
    llm = proposal("inc-1", [("valve-01", "open", {"opening": 0.8}), ("signal-01", "priority"),
                             ("unit-rescue-01", "dispatch")])
    send(svc, PROP, llm)
    [d] = bus.decisions()
    assert d["origin"] == "llm" and d["chosen_proposal"] == llm["id"]
    assert d["alternatives"][0]["origin"] == "rules" and d["alternatives"][0]["score"] < d["score"]
    assert ("unit-rescue-01", "dispatch") in actions_of(d)
    rescue = next(a for a in d["actions"] if a["target_id"] == "unit-rescue-01")
    assert rescue["params"]["zone_id"] == "zone-01"        # dispatch gets the incident zone
    assert d["explanation"].startswith(f"llm proposal (score {d['score']:.2f}) chosen over rules (")
    assert d["mode"] == "normal" and d["risk"] == "high" and d["evidence_sensors"] == ["water-01"]
    assert d["correlation_id"] == "twin-inc-1" and d["id"].startswith("math-")
    [e] = bus.events("decision")
    assert e["summary"] == d["explanation"] and e["incident_id"] == "inc-1"


def test_worse_llm_loses(svc, bus):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    send(svc, PROP, proposal("inc-1", [("signal-01", "priority")]))
    [d] = bus.decisions()
    assert d["origin"] == "rules"
    assert actions_of(d) == [("valve-01", "open"), ("signal-01", "priority")]
    assert d["alternatives"][0]["origin"] == "llm"


def test_budget_used_up_decides_on_rules(svc, bus, clock):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    clock.t = T0 + 7.9
    svc.tick()
    assert bus.decisions() == []
    clock.t = T0 + 8
    svc.tick()
    [d] = bus.decisions()
    assert d["origin"] == "rules" and d["alternatives"] == []
    # a late LLM proposal changes nothing
    send(svc, PROP, proposal("inc-1", [("valve-01", "open"), ("unit-rescue-01", "dispatch")]))
    assert len(bus.decisions()) == 1


@pytest.mark.parametrize("event", ["llm_timeout", "llm_invalid_output", "llm_skipped"])
def test_llm_give_up_event_decides_at_once(make_service, bus, event):
    keys = {"twin": b"k" * 32, "guardian": b"g" * 32}
    svc = make_service(seal_keys=keys)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    body = {"id": "llm-9", "timestamp": "t", "source": "brain", "event": event,
            "incident_id": "inc-1", "summary": "x"}
    bad = seal_multi(body, {"twin": b"x" * 32})
    svc.handle("brain/event", bad, False)
    assert bus.decisions() == []                           # bad HMAC: ignored
    svc.handle("brain/event", seal_multi(body, keys), False)
    [d] = bus.decisions()
    assert d["origin"] == "rules"


def test_plain_event_accepted_without_keys(svc, bus):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    svc.handle("brain/event", body_text({"id": "llm-9", "timestamp": "t", "source": "brain",
                                         "event": "llm_timeout", "incident_id": "inc-1", "summary": "x"}))
    assert len(bus.decisions()) == 1


def test_llm_offline_decides_immediately(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    [d] = bus.decisions()
    assert d["origin"] == "rules"
    assert ("unit-ambulance-02", "dispatch") in actions_of(d)


def test_llm_going_offline_decides_waiting_incidents(svc, bus):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    send(svc, INC, incident("inc-2", "traffic_jam", "zone-03"))
    offline(svc)
    assert {d["incident_id"] for d in bus.decisions()} == {"inc-1", "inc-2"}


def test_llm_proposal_before_incident_is_kept(svc, bus):
    send(svc, PROP, proposal("inc-1", [("valve-01", "open"), ("signal-01", "priority"),
                                       ("unit-rescue-01", "dispatch")]))
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    [d] = bus.decisions()
    assert d["origin"] == "llm"


def test_critical_immediate_then_switch(svc, bus, clock):
    send(svc, INC, incident("inc-1", "flood", "zone-01", severity="critical"))
    [d1] = bus.decisions()
    assert d1["origin"] == "rules"
    assert ("unit-rescue-01", "dispatch") in actions_of(d1)     # critical >= high
    # worse LLM proposal: no change
    clock.t += 2
    send(svc, PROP, proposal("inc-1", [("signal-01", "priority")]))
    assert len(bus.decisions()) == 1
    # better one: switch, only the new actions are sent
    clock.t += 1
    send(svc, PROP, proposal("inc-1", [("valve-01", "open"), ("signal-01", "priority"),
                                       ("signal-02", "priority"), ("unit-rescue-01", "dispatch"),
                                       ("unit-ambulance-01", "dispatch")]))
    d2 = bus.decisions()[-1]
    assert d2["origin"] == "llm" and d2["id"] != d1["id"]
    assert set(actions_of(d2)) == {("signal-02", "priority"), ("unit-ambulance-01", "dispatch")}
    assert d2["score"] > d1["score"]
    assert bus.events("decision_changed")[0]["summary"].startswith("changed (better LLM proposal)")


def test_update_without_trigger_does_not_redecide(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-02"))
    send(svc, INC, incident("inc-1", "flood", "zone-02", status="updated", confidence=0.9))
    assert len(bus.decisions()) == 1
    assert len(bus.proposals("rules", "inc-1")) == 1         # same rule actions: not published again


def test_severity_increase_redecides_with_new_actions_only(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-02"))
    send(svc, INC, incident("inc-1", "flood", "zone-02", status="updated", severity="high"))
    d1, d2 = bus.decisions()
    assert actions_of(d2) == [("unit-rescue-01", "dispatch")]
    assert bus.events("decision_changed")


def test_new_evidence_type_redecides(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "fire", "zone-05"))
    ev = [reading("air-05"), {"source_id": "call-1", "kind": "call", "category": "fire", "trust": 1.0},
          reading("power-05")]
    svc.cfg["decide"]["redecide_on"]["severity_increase"] = False
    send(svc, INC, incident("inc-1", "fire", "zone-05", status="updated", severity="high", evidence=ev))
    assert len(bus.decisions()) == 2


def test_closed_incident_is_forgotten(svc, bus):
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    send(svc, INC, incident("inc-1", "flood", "zone-01", status="closed"))
    assert "inc-1" not in svc.tracks
    send(svc, PROP, proposal("inc-1", [("valve-01", "open")]))
    svc.tick()
    assert bus.decisions() == []


# --- checks of LLM proposals ------------------------------------------------------------


def test_invalid_llm_actions_dropped_and_noted(svc, bus):
    send(svc, "brain/internal/quarantine/valve-01",
         {"id": "in-1", "timestamp": "t", "device_id": "valve-01", "state": "quarantined", "reason": "x"}, True)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    p = Proposal.model_validate(proposal("inc-1", [("valve-99", "open"), ("signal-01", "open"),
                                                   ("valve-01", "open"), ("signal-01", "priority")]))
    s = svc.score(svc.tracks["inc-1"], p)
    assert [a.target_id for a in s.actions] == ["signal-01"]
    assert s.notes[:3] == ["dropped valve-99 (unknown)", "dropped open signal-01 (not allowed)",
                           "dropped valve-01 (quarantined)"]
    # neither proposal ever names the quarantined valve
    send(svc, PROP, proposal("inc-1", [("valve-01", "open"), ("signal-01", "priority")]))
    [d] = bus.decisions()
    assert all(a["target_id"] != "valve-01" for a in d["actions"])


def test_llm_busy_unit_substituted(svc, bus):
    send(svc, ACK, unit_ack("unit-ambulance-02", "on_scene", "zone-06"))
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    p = Proposal.model_validate(proposal("inc-1", [("unit-ambulance-02", "dispatch")]))
    s = svc.score(svc.tracks["inc-1"], p)
    assert [a.target_id for a in s.actions] == ["unit-ambulance-01"]
    assert "unit-ambulance-01 instead of unit-ambulance-02" in s.notes[0]


# --- safe mode, trust -------------------------------------------------------------------


def test_safe_mode_prefers_signal_over_valve_on_one_sensor(svc, bus):
    guardian(svc, "offline")
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    [d] = bus.decisions()
    assert d["mode"] == "safe"
    assert actions_of(d) == [("signal-01", "priority")]
    assert d["risk"] == "low"
    assert "dropped open valve-01" in d["explanation"] and "[safe mode]" in d["explanation"]


def test_safe_mode_two_sensor_types_allow_valve(svc, bus):
    guardian(svc, "offline")
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01",
                            evidence=[reading("water-01"), reading("traffic-speed-01", value=5)]))
    [d] = bus.decisions()
    assert d["mode"] == "safe" and ("valve-01", "open") in actions_of(d)


def test_unverified_incident_is_safe_mode(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01", verified=False))
    [d] = bus.decisions()
    assert d["mode"] == "safe" and actions_of(d) == [("signal-01", "priority")]


def test_guardian_back_online_normal_mode(svc, bus):
    guardian(svc, "offline")
    guardian(svc, "online")
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    assert bus.decisions()[0]["mode"] == "normal"


def test_trust_zero_evidence_skipped(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01", evidence=[reading("water-01", trust=0.0)]))
    assert bus.decisions() == []
    [e] = bus.events("decision_skipped")
    assert "trust 0" in e["summary"] and e["incident_id"] == "inc-1"
    # the same reason is not repeated on every update
    send(svc, INC, incident("inc-1", "flood", "zone-01", status="updated",
                            evidence=[reading("water-01", trust=0.0)]))
    assert len(bus.events("decision_skipped")) == 1


def test_guardian_rejected_verdict_overrides_detection_trust(svc, bus):
    send(svc, "brain/internal/trust/water-01",
         {"id": "in-1", "timestamp": "t", "correlation_id": "c", "sensor_id": "water-01",
          "verdict": "rejected", "trust_score": None, "reason": "out_of_range"}, True)
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    assert bus.decisions() == [] and bus.events("decision_skipped")


def test_low_trust_lowers_score(make_service, bus):
    svc = make_service()
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    send(svc, INC, incident("inc-2", "flood", "zone-02", evidence=[reading("water-02", trust=0.4)]))
    d1, d2 = bus.decisions()
    # same P (both port zones, same confidence): only the risk penalty differs
    assert d2["score"] < d1["score"]


# --- units ------------------------------------------------------------------------------


def _b5(svc, clock, first: str):
    """Fire (high, zone-05) and accident (medium, hospital zone-04): one ambulance free."""
    send(svc, ACK, unit_ack("unit-ambulance-02", "on_scene", "zone-06"))
    fire = incident("inc-fire", "fire", "zone-05", severity="high")
    acc = incident("inc-acc", "accident", "zone-04")
    for inc in ((fire, acc) if first == "fire" else (acc, fire)):
        send(svc, INC, inc)
        clock.t += 1


def test_b5_waiting_lower_priority_leaves_the_ambulance(svc, bus, clock):
    _b5(svc, clock, "fire")
    clock.t = T0 + 8
    svc.tick()                                             # fire decides first
    [d_fire] = bus.decisions("inc-fire")
    assert all(a["target_id"] != "unit-ambulance-01" for a in d_fire["actions"])
    assert "unit-ambulance-01 left for inc-acc (P " in d_fire["explanation"]
    clock.t = T0 + 9
    svc.tick()
    [d_acc] = bus.decisions("inc-acc")
    assert ("unit-ambulance-01", "dispatch") in actions_of(d_acc)


def test_b5_decided_higher_priority_keeps_the_ambulance(svc, bus, clock):
    _b5(svc, clock, "acc")
    clock.t = T0 + 8
    svc.tick()                                             # accident decides first
    [d_acc] = bus.decisions("inc-acc")
    assert ("unit-ambulance-01", "dispatch") in actions_of(d_acc)
    clock.t = T0 + 9
    svc.tick()
    [d_fire] = bus.decisions("inc-fire")
    assert all(a["target_id"] != "unit-ambulance-01" for a in d_fire["actions"])
    assert "unit-ambulance-01 busy with inc-acc (P " in d_fire["explanation"]
    assert ("unit-fire-01", "dispatch") in actions_of(d_fire)


def test_unit_free_again_goes_to_waiting_incident(svc, bus, clock):
    _b5(svc, clock, "acc")
    clock.t = T0 + 9
    svc.tick()
    send(svc, ACK, unit_ack("unit-ambulance-01", "en_route"))
    send(svc, ACK, unit_ack("unit-ambulance-01", "on_scene", "zone-04"))
    send(svc, ACK, unit_ack("unit-ambulance-01", "free", "zone-04"))
    d = bus.decisions("inc-fire")[-1]
    assert actions_of(d) == [("unit-ambulance-01", "dispatch")]
    assert bus.events("decision_changed")


def test_preemption_recalls_unit_of_much_lower_priority(svc, bus, clock):
    offline(svc)
    send(svc, "brain/internal/quarantine/unit-ambulance-02",
         {"id": "in-1", "timestamp": "t", "device_id": "unit-ambulance-02", "state": "quarantined", "reason": "x"})
    send(svc, INC, incident("inc-low", "accident", "zone-03", severity="low", confidence=0.5))
    assert ("unit-ambulance-01", "dispatch") in actions_of(bus.decisions("inc-low")[0])
    send(svc, ACK, unit_ack("unit-ambulance-01", "en_route"))
    send(svc, INC, incident("inc-crit", "accident", "zone-04", severity="critical", confidence=1.0))
    d = bus.decisions("inc-crit")[0]
    assert ("unit-ambulance-01", "recall") in actions_of(d)
    assert "recall unit-ambulance-01 from inc-low (P 8.00 >= 1.5 x 0.50)" in d["explanation"]
    send(svc, ACK, unit_ack("unit-ambulance-01", "returning"))
    send(svc, ACK, unit_ack("unit-ambulance-01", "free", "zone-03"))
    d2 = bus.decisions("inc-crit")[-1]
    assert actions_of(d2) == [("unit-ambulance-01", "dispatch")]
    assert d2["actions"][0]["params"] == {"zone_id": "zone-04"}


def test_no_preemption_below_ratio(svc, bus):
    offline(svc)
    send(svc, "brain/internal/quarantine/unit-ambulance-02",
         {"id": "in-1", "timestamp": "t", "device_id": "unit-ambulance-02", "state": "quarantined", "reason": "x"})
    send(svc, INC, incident("inc-a", "accident", "zone-03"))
    send(svc, ACK, unit_ack("unit-ambulance-01", "en_route"))
    send(svc, INC, incident("inc-b", "accident", "zone-06"))
    d = bus.decisions("inc-b")[0]
    assert all(a["target_id"] != "unit-ambulance-01" for a in d["actions"])
    assert "busy with inc-a" in d["explanation"]


# --- re-decide after failure ------------------------------------------------------------


def test_failed_action_redecides_without_that_target(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    [d1] = bus.decisions()
    send(svc, OUT, outcome("inc-1", d1["id"], "unit-ambulance-02", "dispatch", "failed"))
    svc.tick()                                               # outcomes are coalesced per tick
    d2 = bus.decisions()[-1]
    assert actions_of(d2) == [("unit-ambulance-01", "dispatch")]   # no signal sent again
    [e] = bus.events("decision_changed")
    assert "unit-ambulance-02 failed" in e["summary"]
    assert "unit-ambulance-02" not in str(bus.proposals("rules", "inc-1")[-1]["actions"])


def test_refused_action_without_alternative_is_reported(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    [d1] = bus.decisions()
    send(svc, OUT, outcome("inc-1", d1["id"], "valve-01", "open", "refused"))
    svc.tick()
    assert len(bus.decisions()) == 1
    assert "no new action" in bus.events("decision_skipped")[-1]["summary"]


def test_s3_refusal_does_not_exclude_the_valve_and_it_is_retried_with_more_evidence(svc, bus):
    """B2/B6/B12: the sensor opens the incident 0.2 s before the first call; S3 refuses the valve (one
    source). That is "not yet", not a broken valve: when the call arrives the valve must be proposed again."""
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01", severity="medium"))
    [d1] = bus.decisions()
    assert ("valve-01", "open") in actions_of(d1)
    send(svc, OUT, outcome("inc-1", d1["id"], "valve-01", "open", "refused", rule="S3"))
    svc.tick()
    track = svc.tracks["inc-1"]
    assert "valve-01" not in track.failed and track.redecisions == 0     # no loop on the same evidence
    more = [reading("water-01"), call("call-9", "flood")]
    send(svc, INC, incident("inc-1", "flood", "zone-01", severity="medium", evidence=more, status="updated"))
    assert ("valve-01", "open") in actions_of(bus.decisions()[-1])


def test_real_failure_still_excludes_the_valve(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "flood", "zone-01"))
    [d1] = bus.decisions()
    send(svc, OUT, outcome("inc-1", d1["id"], "valve-01", "open", "failed"))
    svc.tick()
    assert "valve-01" in svc.tracks["inc-1"].failed


def test_outcome_of_unknown_decision_ignored(svc, bus):
    offline(svc)
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    send(svc, OUT, outcome("inc-1", "math-999999", "unit-ambulance-02", "dispatch", "failed"))
    send(svc, OUT, outcome("inc-1", bus.decisions()[0]["id"], "unit-ambulance-02", "dispatch", "acknowledged"))
    assert len(bus.decisions()) == 1


def test_bad_messages_do_not_break_the_service(svc, bus):
    svc.handle(INC, b"not json")
    svc.handle(INC, b'{"incident_id": 1}')
    svc.handle("brain/event", b"[1, 2]")
    offline(svc)
    send(svc, INC, incident("inc-1", "traffic_jam", "zone-03"))
    assert len(bus.decisions()) == 1



def test_several_refusals_of_one_decision_give_one_redecision(svc, bus):
    """S7 (Twin offline) refuses every action of a decision: one re-decision, not one per action."""
    offline(svc)                                             # rules decide at once
    send(svc, INC, incident("inc-1", "flood", "zone-01", severity="high"))
    d1 = bus.decisions()[0]
    for target, action in actions_of(d1):
        send(svc, OUT, outcome("inc-1", d1["id"], target, action, "refused"))
    svc.tick()
    track = svc.tracks["inc-1"]
    assert track.redecisions == 1
    assert len([e for e in bus.events() if e["event"] in ("decision_changed", "decision_skipped")]) == 1


def test_reservation_of_a_dispatch_never_reported_out_is_released(svc, bus, clock):
    offline(svc)                                             # rules decide at once
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    unit = next(t for t, a in actions_of(bus.decisions()[0]) if a == "dispatch")
    send(svc, INC, incident("inc-1", "accident", "zone-03", status="closed"))
    assert unit in svc.assigned                              # incident forgotten, unit still reserved
    clock.t += float(svc.decide_cfg["reservation_ttl_s"]) + 1
    svc.tick()
    assert unit not in svc.assigned                          # no ack ever made it busy: released


def test_refused_dispatch_of_a_forgotten_incident_frees_the_unit(svc, bus):
    offline(svc)                                             # rules decide at once
    send(svc, INC, incident("inc-1", "accident", "zone-03"))
    d1 = bus.decisions()[0]
    unit = next(t for t, a in actions_of(d1) if a == "dispatch")
    send(svc, INC, incident("inc-1", "accident", "zone-03", status="closed"))
    send(svc, OUT, outcome("inc-1", d1["id"], unit, "dispatch", "refused"))
    assert unit not in svc.assigned
