"""Contract tests: EVERY Guardian -> Brain body validates against the kit schemas, every topic
we write is inside guardian/#, topic and body agree, and the kit's example bodies parse."""
import json
import re
from pathlib import Path

import jsonschema
import pytest

from src.adapter.brain import TopicTemplate, brain_reason
from src.adapter.envelope import open_envelope, seal, seal_multi
from src.config import load_settings

SCHEMAS = Path(__file__).resolve().parents[2] / "contracts" / "kit-schemas"
MS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def schema(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def _validate(body, name):
    jsonschema.validate(body, schema(name), cls=jsonschema.Draft202012Validator)


def _exercise(s):
    """Produce every kind of Guardian output: trusted, rejected (jump, range, bad_hmac),
    quarantined (readings and inconsistent_ack), released, status online/offline."""
    s.adapter.announce("online")
    for _ in range(15):
        s.tick("calm", falsify=(10, 40))
    s.send_all(s.twin.bad("bad_hmac"))
    s.send_all(s.twin.bad("impossible"))
    s.send(*s.brain.alert("valve-01"))
    s.send(*s.brain.alert("valve-01"))
    s.send(*s.brain.control({"op": "reset"}))
    for _ in range(12):
        s.tick("calm", freeze=("air-04", 1))          # frozen -> suspicious
    s.advance(25)                                     # Twin silent -> refresh


def test_every_brain_bound_body_matches_kit_schema(bstack):
    s = bstack
    _exercise(s)
    seen = set()
    for m in s.brain_out():
        topic, body = m["topic"], m["body"]
        if not (topic.startswith("guardian/verdict/") or topic.startswith("guardian/quarantine/")
                or topic == "guardian/status"):
            continue
        assert m["verified"], topic                       # sealed with guardian-brain.key
        assert set(m["envelope"]) == {"v", "body", "hmac"}  # single reader -> "hmac"
        assert body["source"] == "guardian" and MS.match(body["timestamp"])
        assert body["id"].startswith("gd-test1-") and len(body["id"]) <= 128
        if topic.startswith("guardian/verdict/"):
            _validate(body, "external-verdict")
            assert topic == f"guardian/verdict/{body['sensor_id']}"
            assert body["correlation_id"]
            seen.add(("verdict", body["verdict"], body["reason"]))
        elif topic.startswith("guardian/quarantine/"):
            _validate(body, "external-quarantine")
            assert topic == f"guardian/quarantine/{body['device_id']}" and m["retain"]
            seen.add(("quarantine", body["state"], body["reason"]))
        else:
            _validate(body, "external-status")
            seen.add(("status", body["status"]))
    for want in [("verdict", "trusted", "consistent"), ("verdict", "rejected", "implausible_jump"),
                 ("verdict", "rejected", "out_of_range"), ("verdict", "rejected", "bad_hmac"),
                 ("quarantine", "quarantined", "implausible_jump"),
                 ("quarantine", "quarantined", "inconsistent_ack"),
                 ("quarantine", "released", "reset"), ("status", "online"),
                 ("verdict", "suspicious", "frozen")]:
        assert want in seen, want
    ids = [m["body"]["id"] for m in s.brain_out()]
    assert len(ids) == len(set(ids))


def test_last_will_payload(bstack):
    text = bstack.adapter.status_payload("offline")
    op = open_envelope(text, bstack.k_gb)
    assert op.ok
    _validate(op.body, "external-status")
    assert op.body["status"] == "offline"


def test_every_topic_we_write_is_inside_guardian_acl(bstack):
    _exercise(bstack)
    assert bstack.bus.history
    for topic, _ in bstack.bus.history:
        assert topic.startswith("guardian/"), topic


def test_retained_flags(bstack):
    s = bstack
    _exercise(s)
    for m in s.brain_out():
        if m["topic"].startswith("guardian/verdict/"):
            assert m["retain"] is (m["body"]["reason"] not in ("bad_hmac", "replay"))
        if m["topic"] == "guardian/status":
            assert m["retain"] is True


# ---------------------------------------------------------------- kit example bodies parse
KIT_META = {"id": "tw-000004", "timestamp": "2026-11-14T10:00:00.000Z", "source": "twin",
            "zones": [{"zone_id": "zone-01", "name": "Port", "type": "port", "exposure": "high",
                       "adjacent": ["zone-02"]}],
            "sensors": [{"sensor_id": "water-01", "zone_id": "zone-01", "type": "water_level", "unit": "m",
                         "min": 0, "max": 5, "alert": 1.3}],
            "actuators": [{"actuator_id": "valve-01", "type": "valve", "zone_id": "zone-01",
                           "controls": ["zone-01"], "actions": ["open", "close"]}],
            "units": [{"unit_id": "unit-ambulance-01", "type": "ambulance", "home_zone": "zone-04"}]}
COMMON = {"timestamp": None, "source": "twin"}


def _kit(s, extra, source="twin"):
    return {"id": s.twin._id(), "timestamp": s.twin.reading_body("water_level", "zone-01", 0)["timestamp"],
            "source": source, **extra}


def test_kit_examples_validate_and_parse(make_bstack):
    s = make_bstack(meta=False)
    _validate(KIT_META, "external-meta")
    assert s.send("twin/meta", seal_multi(KIT_META, {"brain": "kb", "guardian": s.k_tg})) == "meta_loaded"

    reading = _kit(s, {"sensor_id": "water-01", "zone_id": "zone-01", "type": "water_level",
                       "value": 0.42, "unit": "m", "origin": "simulated", "seq": 182})
    _validate(reading, "external-reading")
    assert s.send("city/zone-01/sensor/water_level/water-01",
                  seal_multi(reading, {"brain": "kb", "guardian": s.k_tg})) == "verdict_trusted"

    cmd = {"id": "brain-000042", "timestamp": reading["timestamp"], "source": "brain",
           "correlation_id": "tw17-000182", "target_id": "valve-01", "action": "open",
           "parameters": {"opening": 1.0}}
    _validate(cmd, "brain-command")
    assert s.send("brain/cmd/valve-01", seal_multi(cmd, {"twin": "kt", "guardian": s.k_gb})) == "command_new"

    ack = _kit(s, {"actuator_id": "valve-01", "correlation_id": "brain-000042", "result": "done",
                   "state": "open_100"})
    _validate(ack, "external-actuator")
    assert s.send("twin/actuator/valve-01", seal_multi(ack, {"brain": "kb", "guardian": s.k_tg})) == "ack_ok"

    unit = _kit(s, {"unit_id": "unit-ambulance-01", "status": "en_route", "zone_id": "zone-05"})
    _validate(unit, "external-unit")
    assert s.send("twin/unit/unit-ambulance-01",
                  seal_multi(unit, {"brain": "kb", "guardian": s.k_tg})) == "state_change"

    alert = {"id": "brain-000051", "timestamp": reading["timestamp"], "source": "brain",
             "alert": "actuator_no_effect", "target_id": "valve-01",
             "detail": "water still rising 30 s after open"}
    _validate(alert, "brain-alert")
    assert s.send("brain/alert", seal_multi(alert, {"guardian": s.k_gb})) == "alert_ok"

    err = {"id": "in-err-00031", "timestamp": reading["timestamp"], "source": "brain", "code": "E_RANGE",
           "topic": "guardian/verdict/water-01", "message_id": s.adapter.ids.prefix + "000001",
           "detail": "x"}
    _validate(err, "brain-error")
    assert s.send("brain/error", seal(err, s.k_gb)) == "brain_error_ours"
    assert "brain_refused_our_message" in s.audit_actions()


@pytest.mark.parametrize("native,verdict,step,want", [
    ("anomaly: z=0.1", "trusted", None, "consistent"),
    ("plausibility: water_level jumped 0.4->1.8m (step 1.4 > max 1.0 per tick)", "rejected", 3, "implausible_jump"),
    ("plausibility: water_level=99.0m outside physical range [0.0,5.0]", "rejected", 3, "out_of_range"),
    ("replay: duplicate event_id x", "rejected", 2, "replay"),
    ("device_quarantined (pipeline: ok)", "rejected", 5, "quarantined"),
    ("stuck_sensor: identical value 1.0 for 300s", "suspicious", None, "frozen"),
    ("anomaly: z=7.0", "suspicious", None, "anomaly"),
    ("plausibility: unit 'x' invalid for power_load (expected 'kw')", "rejected", 3, "unit_mismatch"),
])
def test_reason_mapping(native, verdict, step, want):
    assert brain_reason(native, verdict, step) == want


def test_topic_template():
    t = TopicTemplate("city/{zone_id}/sensor/{type}/{sensor_id}")
    assert t.subscription == "city/+/sensor/+/+"
    assert t.match("city/zone-01/sensor/water_level/water-01") == {
        "zone_id": "zone-01", "type": "water_level", "sensor_id": "water-01"}
    assert t.match("city/zone-01/call") is None


def test_settings_brain_defaults(monkeypatch):
    monkeypatch.setenv("BRAIN_PROTOCOL_ENABLED", "true")
    monkeypatch.delenv("TOPIC_TIMELINE", raising=False)
    s = load_settings("/nonexistent")
    assert s.topic_timeline == "guardian/timeline"
    assert s.topic_out_verdict == "guardian/verdict/{sensor_id}"
    monkeypatch.setenv("BRAIN_PROTOCOL_ENABLED", "false")
    assert load_settings("/nonexistent").topic_timeline == "triplet/timeline/guardian"


def test_client_ids_unique_and_configurable():
    from src.config import Settings
    from src.mqtt.publisher import client_id
    s = Settings(mqtt_client_id="guardian")
    assert client_id(s, "pub") != client_id(s, "sub")
    assert client_id(Settings(mqtt_client_id_pub="gd-a"), "pub") == "gd-a"
