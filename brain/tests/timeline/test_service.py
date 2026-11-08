"""Normalisation of every topic type into timeline rows, screen state, bad payloads."""

from __future__ import annotations

import json

from src.common.envelope import seal



def one(rows):
    assert len(rows) == 1, rows
    return rows[0]


def test_subscriptions_cover_the_three_layers(h):
    subs = [t for t, _ in h.s.subscriptions()]
    for t in ("brain/event", "brain/error", "brain/alert", "brain/cmd/+", "brain/status",
              "brain/internal/llm-status", "brain/internal/layer-status/+", "brain/internal/decision",
              "brain/internal/proposal", "brain/internal/incident", "brain/control/run",
              "guardian/verdict/+", "guardian/quarantine/+", "guardian/status", "twin/status",
              "twin/actuator/+", "twin/unit/+", "city/+/call", "brain/internal/reading"):
        assert t in subs, t


def test_brain_event_row_is_verified_and_coloured(h):
    r = one(h.brain("brain/event", {"event": "action_refused", "summary": "REFUSED open valve-01: S1",
                                    "correlation_id": "twin-000182", "incident_id": "inc-1"}))
    assert r["layer"] == "BRAIN" and r["level"] == "error" and r["signed"] is True
    assert r["cid"] == "twin-000182" and r["text"].startswith("REFUSED")
    assert h.s.snapshot()["counters"]["refused"] == 1


def test_llm_events_are_on_the_llm_layer(h):
    r = one(h.brain("brain/event", {"event": "llm_timeout", "summary": "LLM gave no proposal"}))
    assert r["layer"] == "LLM" and r["level"] == "warn"


def test_bad_hmac_is_marked(h):
    body = {"id": "out-9", "timestamp": h.clock.iso(), "source": "brain", "event": "x", "summary": "s"}
    r = one(h.s.handle("brain/event", seal(body, b"wrong-key")))
    assert r["signed"] is False
    assert h.s.snapshot()["counters"]["bad_signatures"] == 1


def test_no_keys_means_no_mark(make):
    h = make(keys={})
    r = one(h.brain("brain/event", {"event": "x", "summary": "s"}))
    assert r["signed"] is None


def test_suppressed_events_are_logged_not_shown(h):
    rows = h.brain("brain/event", {"event": "decision", "summary": "decision ..."})
    assert rows == []
    assert any(line["kind"] == "decision" and line["shown"] is False for line in h.lines())


def test_brain_error_row_and_rate_limit(h):
    for i in range(8):
        h.brain("brain/error", {"code": "E_AUTH", "topic": "city/zone-01/sensor/water_level/water-01",
                                "message_id": f"twin-{i}", "detail": "HMAC does not match"})
    shown = [r for r in h.s.recent() if r["kind"] == "input_rejected"]
    assert len(shown) == 5 and shown[0]["level"] == "error" and "E_AUTH" in shown[0]["text"]
    assert h.s.snapshot()["counters"]["errors"] == 8
    assert len([line for line in h.lines() if line["kind"] == "input_rejected"]) == 8


def test_alert_row(h):
    r = one(h.brain("brain/alert", {"alert": "actuator_no_effect", "target_id": "valve-01", "detail": "d",
                                    "correlation_id": "c1"}))
    assert r["level"] == "error" and "actuator_no_effect" in r["text"]


def test_command_then_twin_ack_share_the_chain(h):
    c = one(h.brain("brain/cmd/valve-01", {"id": "brain-000042", "correlation_id": "twin-000182",
                                           "target_id": "valve-01", "action": "open",
                                           "parameters": {"opening": 0.8}}))
    assert c["cid"] == "twin-000182" and "opening=0.8" in c["text"] and c["signed"] is True
    a = one(h.twin("twin/actuator/valve-01", {"actuator_id": "valve-01", "correlation_id": "brain-000042",
                                              "result": "done", "state": "open"}))
    assert a["layer"] == "TWIN" and a["cid"] == "twin-000182" and a["level"] == "ok"
    assert "open valve-01" in a["text"]
    assert [r["seq"] for r in h.s.chain("twin-000182")] == [c["seq"], a["seq"]]


def test_twin_rejected_ack_is_red(h):
    a = one(h.twin("twin/unit/unit-fire-01", {"unit_id": "unit-fire-01", "correlation_id": "brain-1",
                                              "result": "rejected", "reason": "unit_busy"}))
    assert a["level"] == "error" and "unit_busy" in a["text"]


def test_unit_status_updates_the_map(h):
    one(h.twin("twin/unit/unit-fire-01", {"unit_id": "unit-fire-01", "status": "en_route", "zone_id": "zone-01"}))
    z1 = h.s.snapshot()["zones"][0]
    assert "unit-fire-01" in z1["units"]


def test_retained_device_states_update_state_without_rows(h):
    assert h.twin("twin/unit/unit-fire-01", {"unit_id": "unit-fire-01", "status": "free",
                                             "zone_id": "zone-02"}, retained=True) == []


def test_call_row(h):
    r = one(h.twin("city/zone-01/call", {"call_id": "call-1", "zone_id": "zone-01", "category": "fire",
                                         "text": "Smoke <script>"}))
    assert r["layer"] == "TWIN" and "zone-01 Port" in r["text"] and "fire" in r["text"]


def test_verdicts_only_non_trusted_or_changes(h):
    assert h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "trusted",
                                                    "reason": "ok"}) == []
    r = one(h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "rejected",
                                                     "reason": "out_of_range", "trust_score": 0.1}))
    assert r["layer"] == "GUARDIAN" and r["level"] == "error" and r["signed"] is True
    r = one(h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "trusted",
                                                     "reason": "ok"}))
    assert r["level"] == "ok"


def test_quarantine_row_and_map(h):
    r = one(h.guardian("guardian/quarantine/valve-01", {"device_id": "valve-01", "state": "quarantined",
                                                        "reason": "inconsistent"}))
    assert r["level"] == "error" and "quarantined" in r["text"]
    z1 = h.s.snapshot()["zones"][0]
    assert z1["state"] == "quarantine" and z1["quarantined"] == ["valve-01"]


def test_layer_status_and_safe_mode(h):
    one(h.internal("brain/internal/layer-status/guardian", {"layer": "guardian", "status": "online"}, True))
    r = one(h.internal("brain/internal/layer-status/guardian", {"layer": "guardian", "status": "offline",
                                                                "reason": "no verdict for 30 s"}))
    assert r["layer"] == "GUARDIAN" and "SAFE MODE" in r["text"]
    st = h.s.snapshot()
    assert st["safe_mode"] is True and st["layers"]["guardian"]["status"] == "offline"
    assert h.internal("brain/internal/layer-status/guardian", {"layer": "guardian", "status": "offline"}) == []


def test_status_topics(h):
    one(h.twin("twin/status", {"status": "online"}))
    one(h.brain("brain/status", {"status": "online"}))
    one(h.internal("brain/internal/llm-status", {"status": "offline", "model": "m", "reason": "down"}))
    L = h.s.snapshot()["layers"]
    assert L["twin"]["status"] == "online" and L["brain"]["status"] == "online" and L["llm"]["status"] == "offline"


def test_incident_shows_evidence_readings_then_the_incident(h):
    h.internal("brain/internal/reading", {"correlation_id": "twin-000182", "sensor_id": "water-01",
                                          "zone_id": "zone-01", "type": "water_level", "value": 1.85, "unit": "m"})
    rows = h.internal("brain/internal/incident", {
        "correlation_id": "twin-000182", "incident_id": "inc-1", "type": "flood", "zone_id": "zone-01",
        "status": "opened", "confidence": 0.76, "severity": "high", "verified": True,
        "evidence": [{"source_id": "water-01", "kind": "reading", "value": 1.85, "unit": "m", "trust": 1.0},
                     {"source_id": "call-1", "kind": "call", "category": "flood", "trust": 1.0}]})
    assert [r["kind"] for r in rows] == ["evidence", "incident_opened"]
    assert rows[0]["layer"] == "TWIN" and all(r["cid"] == "twin-000182" for r in rows)
    st = h.s.snapshot()
    assert st["incidents"][0]["type"] == "flood" and st["zones"][0]["state"] == "incident"
    # the evidence sensor's next verdict is shown once, even when trusted
    v = one(h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "trusted",
                                                     "reason": "ok", "correlation_id": "twin-000182"}))
    assert v["incident_id"] == "inc-1"
    assert h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "trusted",
                                                    "reason": "ok"}) == []
    # update: no evidence row again
    rows = h.internal("brain/internal/incident", {
        "correlation_id": "twin-000182", "incident_id": "inc-1", "type": "flood", "zone_id": "zone-01",
        "status": "closed", "confidence": 0.2, "severity": "low", "verified": True, "evidence": []})
    assert [r["kind"] for r in rows] == ["incident_closed"]


def test_reading_alert_crossing_and_sparkline(h):
    rows = []
    for v in (0.4, 0.5, 1.4, 1.5, 1.0):
        h.clock.t += 1
        rows += h.internal("brain/internal/reading", {"correlation_id": "c", "sensor_id": "water-01",
                                                      "zone_id": "zone-01", "type": "water_level", "value": v,
                                                      "unit": "m"})
    assert [r["kind"] for r in rows] == ["reading_alert", "reading_normal"]
    spark = h.s.snapshot()["zones"][0]["spark"]
    assert spark["type"] == "water_level" and spark["points"] == [0.4, 0.5, 1.4, 1.5, 1.0]


def test_decision_row_and_panel(h):
    r = one(h.internal("brain/internal/decision", {
        "correlation_id": "twin-000182", "incident_id": "inc-1", "chosen_proposal": "llm-1", "origin": "llm",
        "actions": [{"target_id": "valve-01", "action": "open", "params": {}}], "score": 1.53,
        "alternatives": [{"proposal_id": "rules-1", "origin": "rules", "score": 1.21}], "risk": "high",
        "mode": "normal", "explanation": "upstream valve"}))
    assert "open valve-01" in r["text"] and "llm 1.53" in r["text"] and "rules 1.21" in r["text"]
    d = h.s.snapshot()["decision"]
    assert d["actions"] == ["open valve-01"] and d["alternatives"][0]["score"] == 1.21


def test_internal_proposals_are_no_rows_by_default(h):
    # the rules_proposal and llm_proposal events already show them: no second row for the same proposal
    p = {"correlation_id": "c", "incident_id": "inc-1", "actions": [{"target_id": "valve-01", "action": "open"}],
         "justification": "j"}
    assert h.internal("brain/internal/proposal", {**p, "origin": "llm"}) == []
    assert h.internal("brain/internal/proposal", {**p, "origin": "rules"}) == []


def test_bad_payloads_are_ignored(h):
    before = h.s.counters["messages"]
    for topic, payload in [("brain/event", b"\xff\xfe"), ("brain/event", "not json"), ("brain/event", "[]"),
                           ("brain/internal/decision", '{"actions": 5}'),
                           ("guardian/verdict/x", '{"v": 1, "body": "{bad"}'),
                           ("twin/actuator/a", '{"v": 1, "body": 3}'),
                           ("brain/internal/incident", '{"evidence": [{"kind": "reading"}]}'),
                           ("unknown/topic", "{}")]:
        h.s.handle(topic, payload)
    assert h.s.counters["messages"] == before + 8
    one(h.brain("brain/event", {"event": "ok", "summary": "still alive"}))


def test_plain_json_on_external_topic_is_marked_unsigned(h):
    body = {"id": "x", "timestamp": h.clock.iso(), "source": "brain", "event": "e", "summary": "s"}
    assert one(h.s.handle("brain/event", json.dumps(body)))["signed"] is False


# --- city map ---------------------------------------------------------------------------


def test_map_has_devices_sensors_layout_and_home_zones(h):
    m = h.s.snapshot()["map"]
    assert m["actuators"]["valve-01"] == {"zone_id": "zone-01", "type": "valve", "controls": ["zone-01"],
                                          "state": None, "outcome": None}
    assert m["sensors"]["water-01"] == {"zone_id": "zone-01", "type": "water_level", "alert": 1.3, "verdict": None}
    assert m["layout"]["zone-01"] == [0, 0] and m["sea"] == "west"
    assert h.s.snapshot()["units"]["unit-fire-01"]["home_zone"] == "zone-02"


def test_map_commands_follow_their_ack_then_expire(h):
    h.brain("brain/cmd/valve-01", {"id": "brain-000042", "correlation_id": "twin-1", "target_id": "valve-01",
                                   "action": "open"})
    assert h.s.snapshot()["map"]["commands"] == [{"id": "brain-000042", "target_id": "valve-01", "action": "open",
                                                  "result": None, "age_s": 0.0}]
    h.clock.t += 1
    h.twin("twin/actuator/valve-01", {"actuator_id": "valve-01", "correlation_id": "brain-000042",
                                      "result": "done", "state": "open_100"})
    m = h.s.snapshot()["map"]
    assert m["commands"][0]["result"] == "done" and m["actuators"]["valve-01"]["state"] == "open_100"
    h.clock.t += h.s.cfg["map"]["recent_s"] + 1
    m = h.s.snapshot()["map"]
    assert m["commands"] == [] and m["actuators"]["valve-01"]["state"] == "open_100"   # state stays


def test_map_state_from_retained_actuator_message(h):
    assert h.twin("twin/actuator/valve-01", {"actuator_id": "valve-01", "state": "closed"}, retained=True) == []
    assert h.s.snapshot()["map"]["actuators"]["valve-01"]["state"] == "closed"


def test_map_shows_a_refusal_with_its_rule(h):
    assert h.internal("brain/internal/outcome", {"correlation_id": "twin-1", "incident_id": "inc-1",
                                                 "decision_id": "d-1", "target_id": "valve-01", "action": "open",
                                                 "status": "refused", "rule": "S6"}) == []
    out = h.s.snapshot()["map"]["actuators"]["valve-01"]["outcome"]
    assert out["status"] == "refused" and out["rule"] == "S6" and out["age_s"] == 0.0


def test_map_calls_and_verdicts(h):
    h.twin("city/zone-01/call", {"call_id": "call-1", "zone_id": "zone-01", "category": "flood"})
    h.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "rejected", "reason": "jump"})
    m = h.s.snapshot()["map"]
    assert m["calls"] == [{"zone_id": "zone-01", "category": "flood", "age_s": 0.0}]
    assert m["sensors"]["water-01"]["verdict"] == "rejected"
    h.clock.t += m["call_s"] + 1
    assert h.s.snapshot()["map"]["calls"] == []


def test_map_without_config_still_draws_any_city(make, timeline_cfg):
    """No map section (another Twin's city): no layout, the page places the zones itself."""
    h2 = make(cfg={k: v for k, v in timeline_cfg.items() if k != "map"})
    m = h2.s.snapshot()["map"]
    assert m["layout"] == {} and m["sea"] is None and set(m["actuators"]) == {"valve-01"}


def test_map_case_memory_verdict_on_its_zone_then_expires(h):
    case = {"correlation_id": "twin-1", "zone_id": "zone-01", "incident_type": "fire", "verdict": "nuisance",
            "share": 1.0, "k": 15, "votes": {"nuisance": 15}, "sources": {"toast": 15}, "features": {},
            "sensors": [], "library": "NIST", "summary": "cooking"}
    assert h.internal("brain/internal/case", case) == []          # the memory's brain/event is the row
    assert h.s.snapshot()["map"]["cases"] == {"zone-01": {"verdict": "nuisance", "share": 1.0,
                                                          "votes": {"nuisance": 15}, "k": 15}}
    h.clock.t += h.s.cfg["map"]["case_s"] + 1
    assert h.s.snapshot()["map"]["cases"] == {}


def test_case_events_are_coloured(h):
    r = one(h.brain("brain/event", {"event": "case_nuisance", "summary": "smoke looks like COOKING"}))
    assert r["layer"] == "BRAIN" and r["level"] == "ok"
    r = one(h.brain("brain/event", {"event": "case_fire", "summary": "looks like a REAL FIRE"}))
    assert r["level"] == "warn"
