"""Internal outputs, retained topics, layer status and the interface tests of the spec."""

import json

from .conftest import GUARDIAN_KEY


def retained_topics(h):
    return {t for t, _, _, retain in h.bus.sent if retain}


# IT-1: Brain starts after the Twin -> gets the retained city description

def test_it1_retained_city_description(h):
    h.send_meta(retained=True)
    meta = h.bus.on("brain/internal/meta")
    assert len(meta) == 1 and meta[0]["correlation_id"] == "twin-meta-001"
    assert "brain/internal/meta" in retained_topics(h)
    assert [e["event"] for e in h.bus.on("brain/event")] == ["city_description_received"]


def test_meta_delivered_again_does_not_repeat_timeline_event(h):
    h.send_meta()
    h.send_meta()
    assert len(h.bus.on("brain/event")) == 1


# IT-2: valid reading accepted, published in the internal format

def test_it2_reading_internal_format(ready):
    sent = ready.send_reading(value=2.4, origin="real")
    (r,) = ready.bus.on("brain/internal/reading")
    assert r["id"].startswith("in-")
    assert r["correlation_id"] == sent["id"]
    assert {k: r[k] for k in ("sensor_id", "zone_id", "type", "value", "unit", "origin")} == {
        "sensor_id": "water-level-04", "zone_id": "port", "type": "water_level",
        "value": 2.4, "unit": "m", "origin": "real"}
    assert r["timestamp"].endswith("Z")


def test_origin_absent_when_twin_gives_none(ready):
    ready.send_reading()
    assert "origin" not in ready.bus.on("brain/internal/reading")[0]


def test_call(ready):
    body = ready.body(call_id="call-0012", zone_id="z3", category="fire", text="Smoke coming from the substation")
    ready.send("city/z3/call", body)
    (c,) = ready.bus.on("brain/internal/call")
    assert c["call_id"] == "call-0012" and c["category"] == "fire" and c["correlation_id"] == body["id"]


def test_ack_keeps_command_id(ready):
    body = ready.body(correlation_id="brain-000042", actuator_id="valve-port-up", result="done", state="open_80")
    ready.send("twin/actuator/valve-port-up", body)
    (a,) = ready.bus.on("brain/internal/ack")
    assert a["correlation_id"] == "brain-000042"
    assert a["kind"] == "actuator" and a["target_id"] == "valve-port-up"
    assert a["result"] == "done" and a["state"] == "open_80" and a["message_id"] == body["id"]


def test_verdict_published_retained_per_sensor(ready):
    v = ready.guardian(correlation_id="twin-000182", sensor_id="water-level-04",
                       verdict="suspect", reason="spike")   # spec v0.1 name
    ready.send("guardian/verdict/water-level-04", v, key=GUARDIAN_KEY)
    (t,) = ready.bus.on("brain/internal/trust/water-level-04")
    assert t["verdict"] == "suspicious"
    assert t["trust_score"] is None
    assert t["correlation_id"] == "twin-000182"
    assert "brain/internal/trust/water-level-04" in retained_topics(ready)


def test_verdict_trust_score_passed_through(ready):
    v = ready.guardian(sensor_id="water-level-04", verdict="rejected", reason="out_of_range", trust_score=0.12)
    ready.send("guardian/verdict/water-level-04", v, key=GUARDIAN_KEY)
    assert ready.bus.on("brain/internal/trust/")[0]["trust_score"] == 0.12


def test_quarantine_retained_and_on_timeline(ready):
    q = ready.guardian(device_id="valve-port-up", state="quarantined", reason="inconsistent")
    ready.send("guardian/quarantine/valve-port-up", q, key=GUARDIAN_KEY)
    (m,) = ready.bus.on("brain/internal/quarantine/valve-port-up")
    assert m["state"] == "quarantined"
    assert "brain/internal/quarantine/valve-port-up" in retained_topics(ready)
    assert ready.bus.on("brain/event")[-1]["event"] == "device_quarantined"


# IT-10 / IT-11: layer offline

def test_it10_guardian_status_offline(ready):
    ready.send("guardian/status", ready.guardian(status="online"), key=GUARDIAN_KEY)
    ready.send("guardian/status", ready.guardian(status="offline"), key=GUARDIAN_KEY)
    statuses = [m["status"] for m in ready.bus.on("brain/internal/layer-status/guardian")]
    assert statuses == ["online", "offline"]
    assert "brain/internal/layer-status/guardian" in retained_topics(ready)


def test_it11_twin_status_offline(ready):
    ready.send("twin/status", ready.body(status="online"))
    ready.send("twin/status", ready.body(status="offline"))
    assert [m["status"] for m in ready.bus.on("brain/internal/layer-status/twin")] == ["online", "offline"]
    assert ready.bus.on("brain/event")[-1]["event"] == "layer_offline"


def test_last_will_payload_is_old_but_accepted(ready):
    will = ready.guardian(status="offline", timestamp=ready.clock.iso(-3600))
    ready.send("guardian/status", will, key=GUARDIAN_KEY)
    assert ready.bus.on("brain/internal/layer-status/guardian")[-1]["status"] == "offline"


def test_guardian_offline_after_30s_without_verdict(ready):
    ready.send("guardian/status", ready.guardian(status="online"), key=GUARDIAN_KEY)
    v = ready.guardian(sensor_id="water-level-04", verdict="trusted", reason="ok")
    ready.send("guardian/verdict/water-level-04", v, key=GUARDIAN_KEY)
    ready.clock.t += 29
    ready.service.tick()
    assert ready.bus.on("brain/internal/layer-status/guardian")[-1]["status"] == "online"
    ready.clock.t += 2
    ready.service.tick()
    last = ready.bus.on("brain/internal/layer-status/guardian")[-1]
    assert last["status"] == "offline" and "no verdict" in last["reason"]
    # a new verdict brings it back
    v = ready.guardian(sensor_id="water-level-04", verdict="trusted", reason="ok")
    ready.send("guardian/verdict/water-level-04", v, key=GUARDIAN_KEY)
    assert ready.bus.on("brain/internal/layer-status/guardian")[-1]["status"] == "online"


def test_guardian_unknown_at_start_then_offline_if_silent(h):
    h.service.tick()
    assert h.bus.on("brain/internal/layer-status/guardian") == []
    h.clock.t += 31
    h.service.tick()
    assert h.bus.on("brain/internal/layer-status/guardian")[-1]["status"] == "offline"


def test_retained_verdicts_do_not_count_as_guardian_alive(ready):
    v = ready.guardian(sensor_id="water-level-04", verdict="trusted", reason="ok")
    ready.send("guardian/verdict/water-level-04", v, key=GUARDIAN_KEY, retained=True)
    assert len(ready.bus.on("brain/internal/trust/")) == 1
    assert ready.bus.on("brain/internal/layer-status/guardian") == []


# brain/error and brain/status

def test_error_message_format(ready):
    ready.send_reading(value=-5.0)
    (err,) = ready.errors()
    assert err["id"].startswith("in-err-") and err["source"] == "brain"
    assert err["code"] == "E_RANGE"
    assert err["topic"] == "city/port/sensor/water_level/water-level-04"
    assert err["message_id"].startswith("twin-")
    assert err["detail"] == "value -5.0 outside [0.0, 4.0]"


def test_outgoing_messages_are_sealed_for_twin_and_guardian(ready):
    ready.send_reading(value=-5.0)
    topic, payload, _, _ = ready.bus.sent[-1]
    env = json.loads(payload)
    assert topic == "brain/error" and set(env["hmacs"]) == {"twin", "guardian"}


def test_error_publishing_is_rate_limited(ready):
    limit = ready.service.cfg["errors"]["max_published_per_s"]
    for _ in range(limit + 20):
        ready.service.handle("city/port/sensor/water_level/water-level-04", "garbage")
    assert len(ready.errors()) == limit
    ready.clock.t += 1
    ready.service.handle("city/port/sensor/water_level/water-level-04", "garbage")
    assert len(ready.errors()) == limit + 1


def test_status_payload_is_sealed(h):
    env = json.loads(h.service.status_payload("offline"))
    body = json.loads(env["body"])
    assert body["status"] == "offline" and body["source"] == "brain" and "hmacs" in env


def test_a_crash_in_one_message_does_not_stop_the_next(ready, monkeypatch):
    calls = {"n": 0}
    original = ready.service._dispatch

    def flaky(a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        original(a)

    monkeypatch.setattr(ready.service, "_dispatch", flaky)
    ready.send_reading()
    ready.send_reading()
    assert len(ready.bus.on("brain/internal/reading")) == 1


def test_subscriptions_come_from_config(h):
    subs = dict(h.service.subscriptions())
    assert subs["city/+/sensor/+/+"] == 0
    assert subs["guardian/verdict/+"] == 1
    assert "twin/meta" in subs and "guardian/status" in subs
