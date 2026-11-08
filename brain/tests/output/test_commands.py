"""Commands: build, seal, acknowledgements, retry, effect check, idempotency."""

from __future__ import annotations

import json

import pytest

from src.common.envelope import EnvelopeError, parse_envelope, verify_hmac
from src.common.models import BrainAlert, BrainCommand, Outcome, TimelineEvent

from .conftest import GUARDIAN_KEY, TWIN_KEY, WATER2


def send_valve(h, **kw) -> dict:
    h.incident(WATER2)
    h.decision(("valve-01", "open", {"opening": 0.8}), **kw)
    return h.commands()[-1]


def test_command_is_built_from_the_decision(h):
    cmd = send_valve(h)
    BrainCommand.model_validate(cmd)
    assert cmd["id"] == "brain-000042"
    assert cmd["source"] == "brain" and cmd["correlation_id"] == "twin-000182"
    assert cmd["target_id"] == "valve-01" and cmd["action"] == "open" and cmd["parameters"] == {"opening": 0.8}
    topic, _, qos, retain = h.bus.raw("brain/cmd/")[0]
    assert topic == "brain/cmd/valve-01" and qos == 1 and retain is False
    [o] = h.bus.outcomes("sent")
    Outcome.model_validate(o)
    assert o["command_id"] == "brain-000042" and o["decision_id"].startswith("math-")
    [e] = h.bus.events("command_sent")
    TimelineEvent.model_validate(e)
    assert "valve-01" in e["summary"] and "zone-01 (Port)" in e["summary"]


def test_command_is_sealed_for_twin_and_guardian(h):
    send_valve(h)
    _, payload, _, _ = h.bus.raw("brain/cmd/")[0]
    env = parse_envelope(payload)
    verify_hmac(env, TWIN_KEY, "twin")
    verify_hmac(env, GUARDIAN_KEY, "guardian")
    with pytest.raises(EnvelopeError):
        verify_hmac(env, b"wrong", "twin")
    # events are sealed too
    _, ev_payload, _, _ = h.bus.raw("brain/event")[0]
    verify_hmac(parse_envelope(ev_payload), TWIN_KEY, "twin")


def test_internal_outcome_is_plain_json(h):
    send_valve(h)
    _, payload, _, _ = h.bus.raw("brain/internal/outcome")[0]
    assert "body" not in json.loads(payload)


def test_dispatch_zone_is_the_incident_zone(h):
    h.incident([{"source_id": "air-01", "kind": "reading", "trust": 1.0},
                {"source_id": "call-1", "kind": "call", "category": "fire", "trust": 1.0}], type_="fire")
    h.decision(("unit-fire-01", "dispatch", {}))
    assert h.commands()[0]["parameters"] == {"zone_id": "zone-01"}
    h.decision(("unit-fire-01", "dispatch", {"zone_id": "zone-02"}))   # cooldown refuses the second
    assert h.bus.outcomes("refused")[0]["rule"] == "S4"


def test_dispatch_wrong_zone_is_replaced(make):
    h = make()
    h.incident([{"source_id": "air-01", "kind": "reading", "trust": 1.0},
                {"source_id": "call-1", "kind": "call", "category": "fire", "trust": 1.0}], type_="fire")
    h.decision(("unit-fire-01", "dispatch", {"zone_id": "zone-02"}))
    assert h.commands()[0]["parameters"]["zone_id"] == "zone-01"


def test_ack_done_gives_acknowledged(h):
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01", state="open")
    [o] = h.bus.outcomes("acknowledged")
    assert o["command_id"] == cmd["id"] and o["detail"] == "state open"
    assert len(h.bus.events("command_acknowledged")) == 1
    assert cmd["id"] not in h.s.pending


def test_ack_rejected_gives_failed_with_reason_and_alert(h):
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01", "rejected", reason="quarantined", state=None)
    [o] = h.bus.outcomes("failed")
    assert o["detail"] == "quarantined"
    assert len(h.bus.events("command_failed")) == 1
    [a] = h.bus.on("brain/alert")
    BrainAlert.model_validate(a)
    assert a["alert"] == "command_rejected" and a["target_id"] == "valve-01"


def test_state_update_and_foreign_acks_are_ignored(h):
    cmd = send_valve(h)
    h.ack("brain-999999", "valve-01")                       # not ours
    h.ack(cmd["id"], "valve-02")                            # wrong target
    h.send("brain/internal/ack", {"id": "in-9", "timestamp": "t", "correlation_id": "twin-5",
                                  "message_id": "twin-5", "kind": "actuator", "target_id": "valve-01",
                                  "state": "open"})         # plain state update
    assert h.bus.outcomes("acknowledged") == [] and cmd["id"] in h.s.pending


def test_duplicate_ack_is_ignored(make, output_cfg):
    output_cfg["effects"]["enabled"] = False
    h = make(cfg=output_cfg)
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01")
    h.ack(cmd["id"], "valve-01")
    h.ack(cmd["id"], "valve-01", "failed", reason="late")
    assert len(h.bus.outcomes("acknowledged")) == 1 and h.bus.outcomes("failed") == []


def test_no_ack_retries_once_with_same_id_then_fails_with_alert(h):
    cmd = send_valve(h)
    first = h.bus.raw("brain/cmd/")[0]
    h.advance(3.5)
    assert len(h.bus.raw("brain/cmd/")) == 1
    h.advance(1.0)                                          # 4 s: retry
    cmds = h.bus.raw("brain/cmd/")
    assert len(cmds) == 2
    b1, b2 = (json.loads(json.loads(c[1])["body"]) for c in (first, cmds[1]))
    # same id and command (the Twin executes an id once), fresh timestamp (not refused as stale)
    assert b2["id"] == b1["id"] and {k: b2[k] for k in ("target_id", "action", "parameters")} ==         {k: b1[k] for k in ("target_id", "action", "parameters")}
    assert b2["timestamp"] > b1["timestamp"]
    assert len(h.bus.events("command_retried")) == 1
    assert h.bus.outcomes("failed") == []
    h.advance(4.0)                                          # second timeout: failed
    assert len(h.bus.raw("brain/cmd/")) == 2
    [o] = h.bus.outcomes("failed")
    assert o["detail"] == "no_ack" and o["command_id"] == cmd["id"]
    [a] = h.bus.on("brain/alert")
    assert a["alert"] == "actuator_not_responding" and a["target_id"] == "valve-01"
    assert len(h.bus.events("command_failed")) == 1
    h.ack(cmd["id"], "valve-01")                            # late ack after failure: ignored
    assert h.bus.outcomes("acknowledged") == []


def test_ack_after_retry_is_acknowledged(h):
    cmd = send_valve(h)
    h.advance(5)
    h.ack(cmd["id"], "valve-01")
    assert len(h.bus.outcomes("acknowledged")) == 1
    h.advance(10)
    assert h.bus.on("brain/alert") == []


def test_duplicate_decision_is_never_executed_twice(h):
    h.incident(WATER2)
    h.decision(("signal-01", "priority", {}), decision_id="math-000020")
    h.clock.t += 120                                        # past the cooldown too
    h.decision(("signal-01", "priority", {}), decision_id="math-000020")
    assert len(h.commands()) == 1 and len(h.bus.outcomes()) == 1


def test_each_action_of_a_decision_is_handled(h):
    h.incident(WATER2)
    h.decision(("valve-01", "open", {}), ("signal-01", "open", {}), ("signal-01", "priority", {}))
    assert [o["status"] for o in h.bus.outcomes()] == ["sent", "refused", "sent"]
    ids = [c["id"] for c in h.commands()]
    assert ids == ["brain-000042", "brain-000043"]


def test_bad_payloads_never_raise(h):
    h.s.handle("brain/internal/decision", b"not json")
    h.s.handle("brain/internal/decision", b"[1,2]")
    h.s.handle("brain/internal/decision", json.dumps({"id": "x"}))
    h.s.handle("brain/internal/ack", json.dumps({"correlation_id": 3}))
    h.s.handle("brain/internal/layer-status/guardian", "{}")
    assert h.commands() == []


def test_extra_decision_fields_are_tolerated(h):
    h.incident(WATER2)
    h.send("brain/internal/decision", {
        "id": "math-1", "timestamp": "t", "correlation_id": "c", "incident_id": "inc-0001",
        "chosen_proposal": "p", "origin": "rules", "actions": [{"target_id": "signal-01", "action": "priority"}],
        "score": 1.0, "risk": "low", "mode": "normal", "explanation": "x", "new_field": 1})
    assert len(h.commands()) == 1


# --- effect check ------------------------------------------------------------------------

def rising(h, start: float, n: int, step: float = 0.15, sensor: str = "water-01") -> float:
    v = start
    for _ in range(n):
        h.reading(sensor, "zone-01", "water_level", v)
        h.clock.t += 1
        v += step
    return v


def test_effect_confirmed_when_water_turns_down(h):
    v = rising(h, 1.4, 6)                                   # last reading 2.15
    cmd = send_valve(h)
    h.clock.t += 1
    h.reading("water-01", "zone-01", "water_level", v)      # still rising before the valve acts
    h.ack(cmd["id"], "valve-01")
    h.advance(1.0)
    assert h.bus.events("effect_confirmed") == []
    h.reading("water-01", "zone-01", "water_level", v - 0.15)   # 0.15 below the peak
    h.advance(0.5)
    [e] = h.bus.events("effect_confirmed")
    assert "water-01 fell" in e["summary"]
    assert h.bus.outcomes("failed") == []
    h.advance(40)
    assert h.bus.on("brain/alert") == []


def test_flood_levelling_off_is_not_an_effect(h):
    rising(h, 1.4, 6)
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01")
    for i in range(31):                                     # plateau with +-2 % noise, stuck valve
        h.reading("water-01", "zone-01", "water_level", 2.5 * (1.02 if i % 2 else 0.98))
        h.advance(1.0)
    assert h.bus.events("effect_confirmed") == []
    assert h.bus.outcomes("failed")[0]["detail"] == "no_effect"


def test_no_effect_gives_failed_and_alert(h):
    v = rising(h, 1.4, 6)
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01")
    for _ in range(31):                                     # stuck valve: the level keeps rising
        h.reading("water-01", "zone-01", "water_level", v)
        v += 0.15
        h.advance(1.0)
    [o] = h.bus.outcomes("failed")
    assert o["detail"] == "no_effect" and o["command_id"] == cmd["id"]
    [a] = h.bus.on("brain/alert")
    assert a["alert"] == "actuator_no_effect"
    [e] = h.bus.events("command_failed")
    assert "NO EFFECT" in e["summary"]


def test_effect_rises_for_signal_priority(h):
    for _ in range(5):
        h.reading("traffic-speed-01", "zone-01", "traffic_speed", 5.0)
        h.clock.t += 1
    h.incident(WATER2)
    h.decision(("signal-01", "priority", {}))
    cmd = h.commands()[-1]
    h.ack(cmd["id"], "signal-01", state="priority")
    h.reading("traffic-speed-01", "zone-01", "traffic_speed", 13.0)
    h.advance(0.5)
    assert len(h.bus.events("effect_confirmed")) == 1


def test_no_readings_means_no_effect_check(h):
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01")
    h.advance(40)
    assert h.bus.outcomes("failed") == [] and h.bus.events("effect_confirmed") == []


def test_effect_check_can_be_disabled(make, output_cfg):
    output_cfg["effects"]["enabled"] = False
    h = make(cfg=output_cfg)
    v = rising(h, 1.4, 6)
    cmd = send_valve(h)
    h.ack(cmd["id"], "valve-01")
    for _ in range(31):
        h.reading("water-01", "zone-01", "water_level", v)
        v += 0.15
        h.advance(1.0)
    assert h.bus.outcomes("failed") == []


def test_auto_command_ids_never_repeat_across_restarts():
    from src.output.service import CommandIds
    first = CommandIds("brain", "auto", 1_800_000_000.0)
    used = {first.next() for _ in range(500)}                  # a burst before a crash
    again = CommandIds("brain", "auto", 1_800_000_002.0)       # restarted 2 s later
    assert not used & {again.next() for _ in range(500)}
    assert CommandIds("brain", 7, 0).next() == "brain-000007"
