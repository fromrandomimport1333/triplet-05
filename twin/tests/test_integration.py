"""Tests of the City Brain protocol profile (protocol.py, translate.py, bridge.py). No broker, no SUMO: the bridge's MQTT
client is replaced by a recorder, and the Twin runs in --fake mode and is stepped by hand."""
import json
import os
import sys
import time

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TWIN, "src"))
import bridge as br  # noqa: E402
import protocol as pr  # noqa: E402
import twin as tw  # noqa: E402

BRAIN_KEY, GUARD_KEY = b"brain-key-text", b"guardian-key-text"


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, retain))

    def tls_set(self, *a, **k): pass
    def username_pw_set(self, *a, **k): pass
    def reconnect_delay_set(self, *a, **k): pass
    def will_set(self, *a, **k): self.will = (a, k)
    def subscribe(self, *a, **k): pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = yaml.safe_load(open(os.path.join(TWIN, "city.yaml"), encoding="utf-8"))
    cfg["mqtt"]["enabled"] = False
    cfg["schemas_dir"] = os.path.join(TWIN, "..", "docs", "schemas")
    cfg["api"].update(hmac_key="k")
    (tmp_path / "brain.key").write_text("brain-key-text\n")
    (tmp_path / "guardian.key").write_text("guardian-key-text\n")
    icfg = cfg["integration"]
    icfg.update(enabled=True, run_id="tw1")
    icfg["mqtt"].update(tls=False, username="")
    icfg["keys"] = {"brain": str(tmp_path / "brain.key"), "guardian": str(tmp_path / "guardian.key")}
    monkeypatch.setattr(br.mqtt, "Client", lambda *a, **k: FakeClient())
    t = tw.Twin(cfg, str(tmp_path), fake=True, speed=1000)
    b = br.Bridge(t, icfg, str(tmp_path))
    t.bridge = b
    b._on_connect(b.client, None, None, 0)
    b.client.sent.clear()
    return t, b


def sent(b, prefix):
    out = []
    for topic, payload, retain in b.client.sent:
        if topic.startswith(prefix):
            env = json.loads(payload)
            out.append((topic, json.loads(env["body"]), env, retain))
    return out


def command(b, target, action, params=None, cid="brain-x-000001", **over):
    body = {"id": cid, "timestamp": pr.iso_ms(), "source": "brain", "correlation_id": "tw1-water-07-5",
            "target_id": target, "action": action, "parameters": params or {}}
    body.update(over)
    payload = pr.seal(body, {"twin": BRAIN_KEY, "guardian": b"g"})
    b._on_message(None, None, type("M", (), {"topic": "brain/cmd/" + target, "payload": payload})())


# ---------- protocol ----------
def test_hmac_matches_the_published_test_vector():
    text = ('{"id":"tw-test-0001","timestamp":"2026-11-14T10:00:00.000Z","source":"twin","sensor_id":"water-01",'
            '"zone_id":"zone-01","type":"water_level","value":0.42,"unit":"m","origin":"simulated","seq":1}')
    assert pr.tag(text, b"test-key-do-not-use") == "503688fdc8bfb862d0617d77924a992ca83840855b3fd987df36aeeb2e12678d"


def test_envelope_round_trip_and_clear_errors():
    env = pr.seal({"a": 1}, {"twin": b"k1", "guardian": b"k2"})
    assert json.loads(env)["body"] == '{"a":1}'
    assert pr.open_envelope(env, b"k1", "twin") == {"a": 1}
    with pytest.raises(pr.ProtocolError) as e:
        pr.open_envelope(env, b"wrong", "twin")
    assert e.value.code == "E_AUTH"
    with pytest.raises(pr.ProtocolError) as e:
        pr.open_envelope(json.dumps({"v": 1, "body": {"a": 1}, "hmac": "00"}), b"k1", "twin")
    assert e.value.code == "E_FORMAT" and "nested" in e.value.detail
    with pytest.raises(pr.ProtocolError) as e:
        pr.open_envelope(pr.seal({"a": 1}, {"brain": b"k1", "guardian": b"k2"}), b"k1", "twin")
    assert e.value.code == "E_AUTH" and "hmacs.twin" in e.value.detail
    with pytest.raises(pr.ProtocolError) as e:
        pr.check_fresh({"timestamp": "2020-01-01T00:00:00.000Z"}, time.time(), 10)
    assert e.value.code == "E_STALE"


def test_ids_are_normalised():
    assert pr.normalize_id("CAM_01") == "cam-01" and pr.normalize_id("zone_a") == "zone-a"


# ---------- meta and readings ----------
def test_meta_is_valid_for_the_partners(rig):
    t, b = rig
    meta = b.map.meta()
    for key in ("zones", "sensors", "actuators", "units"):
        for item in meta[key]:
            for field, value in item.items():
                if field.endswith("_id") or field == "home_zone":
                    assert pr.ID_RE.match(value), (key, field, value)
    types = {s["type"] for s in meta["sensors"]}
    assert types == {"traffic_speed", "water_level", "air_quality", "power_load"}      # rain hidden, camera = speed only
    assert {a["type"] for a in meta["actuators"]} == {"valve", "traffic_signal", "power_grid"}
    assert {u["type"] for u in meta["units"]} == {"ambulance", "police", "fire_truck"}
    assert all(s["min"] <= s["max"] for s in meta["sensors"])


def test_connect_publishes_meta_first_then_status_and_will_is_offline(rig):
    t, b = rig
    b.client.sent.clear()
    b._on_connect(b.client, None, None, 0)
    topics = [x[0] for x in b.client.sent]
    assert topics[0] == "twin/meta" and topics[1] == "twin/status"
    assert b.client.sent[0][2] and b.client.sent[1][2]                      # retained
    will = json.loads(json.loads(b.client.will[0][1])["body"])
    assert will["status"] == "offline"


def test_readings_use_the_partner_topic_units_and_leak_nothing(rig):
    t, b = rig
    for _ in range(3):
        t.step_once()
    t.start_fault("WATER_07", "spoof", 4.2, 30)
    t.step_once()
    out = sent(b, "city/")
    assert out
    for topic, body, env, _ in out:
        assert topic == "city/%s/sensor/%s/%s" % (body["zone_id"], body["type"], body["sensor_id"])
        assert pr.ID_RE.match(body["sensor_id"]) and body["source"] == "twin" and body["origin"] == "simulated"
        assert set(env) == {"v", "body", "hmacs"} and set(env["hmacs"]) == {"brain", "guardian"}
        assert not ({"fault", "sig", "kind", "quarantined", "contract_version"} & set(body))
        assert pr.tag(env["body"], BRAIN_KEY) == env["hmacs"]["brain"]
    spoofed = [x for x in out if x[1]["sensor_id"] == "water-07"][-1][1]
    assert spoofed["value"] == 4.2 and spoofed["unit"] == "m"
    power = [x for x in out if x[1]["type"] == "power_load"][0][1]
    assert power["unit"] == "kW" and power["value"] > 100                   # % converted with scale
    assert not [x for x in b.client.sent if x[0] == "city/faults"]


def test_calls_become_partner_calls(rig):
    t, b = rig
    from fastapi.testclient import TestClient
    c = TestClient(tw.build_app(t))
    c.post("/emergency_calls", headers={"X-API-Key": t.cfg["api"]["test_key"]},
           json={"call_type": "crime", "lonlat": [10.18, 36.80], "description": "x" * 400})
    topic, body, _, _ = sent(b, "city/")[-1]
    assert topic.endswith("/call") and body["category"] == "other" and len(body["text"]) == 280
    assert pr.ID_RE.match(body["call_id"]) and pr.ID_RE.match(body["zone_id"])


# ---------- commands ----------
def test_valve_command_is_acknowledged_once_and_a_retry_gets_the_same_answer(rig):
    t, b = rig
    command(b, "valve-03", "open", {"opening": 1.0})
    assert t.cmds.qsize() == 1
    t.step_once()
    assert t.valve_open["VALVE_03"] is True
    topic, ack, env, _ = sent(b, "twin/actuator/")[-1]
    assert topic == "twin/actuator/valve-03" and ack["correlation_id"] == "brain-x-000001"
    assert ack["result"] == "done" and ack["state"] == "open" and ack["actuator_id"] == "valve-03"
    assert set(env["hmacs"]) == {"brain", "guardian"}
    b.client.sent.clear()
    command(b, "valve-03", "close")                       # same id again: a retry, not applied
    assert t.cmds.qsize() == 0 and t.valve_open["VALVE_03"] is True
    assert sent(b, "twin/actuator/")[0][1] == ack         # the old answer again


def test_rejections_are_acknowledged_with_a_reason(rig):
    t, b = rig
    command(b, "valve-03", "teleport", cid="c1")
    r = sent(b, "twin/actuator/")[-1][1]
    assert r["result"] == "rejected" and "unsupported_action" in r["reason"]
    command(b, "civpro-1", "land", cid="c2")
    r = sent(b, "twin/unit/")[-1][1]
    assert r["result"] == "rejected" and "dispatch and recall" in r["reason"]
    command(b, "civpro-1", "dispatch", {"zone_id": "nowhere"}, cid="c3")
    assert "unknown_zone" in sent(b, "twin/unit/")[-1][1]["reason"]
    assert t.cmds.qsize() == 0


def test_bad_envelopes_are_refused_and_nothing_is_queued(rig, capsys):
    t, b = rig
    body = {"id": "c9", "timestamp": pr.iso_ms(), "source": "brain", "target_id": "valve-03", "action": "open"}
    for payload, expect in [(pr.seal(body, {"twin": b"WRONG", "guardian": b"g"}), "E_AUTH"),
                            (pr.seal(dict(body, timestamp="2020-01-01T00:00:00.000Z", id="c10"), {"twin": BRAIN_KEY, "guardian": b"g"}), "E_STALE"),
                            (pr.seal({k: v for k, v in body.items() if k != "action"}, {"twin": BRAIN_KEY, "guardian": b"g"}), "E_FORMAT"),
                            ("not json", "E_FORMAT")]:
        b._on_message(None, None, type("M", (), {"topic": "brain/cmd/valve-03", "payload": payload})())
        assert expect in capsys.readouterr().out
    assert t.cmds.qsize() == 0 and b.stats["refused"] == 4


def test_dispatch_targets_the_named_unit_and_reports_unit_status(rig):
    t, b = rig
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-a"}, cid="d1")
    t.step_once()
    ack = sent(b, "twin/unit/civpro-1")[0][1]
    assert ack["result"] == "done" and ack["status"] == "en_route" and ack["zone_id"] == "zone-a" and ack["correlation_id"] == "d1"
    t.t += 10000                                          # time passes: arrival, then time on scene
    t.process_arrivals()
    t.bridge.tick()
    statuses = [x[1]["status"] for x in sent(b, "twin/unit/civpro-1")]
    assert statuses[-3:] == ["on_scene", "returning", "free"]
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-a"}, cid="d2")
    command(b, "ambu-1", "dispatch", {"zone_id": "zone-a"}, cid="d3")
    t.step_once()
    assert t.units["AMBU_1"]["busy_until"] is not None and t.units["AMBU_2"]["busy_until"] is None


def test_unit_command_for_a_busy_unit_fails_with_a_reason(rig):
    t, b = rig
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-a"}, cid="d1")
    t.step_once()
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-b"}, cid="d2")
    t.step_once()
    last = sent(b, "twin/unit/civpro-1")[-1][1]
    assert last["correlation_id"] == "d2" and last["result"] == "failed" and "busy" in last["reason"]


def test_quarantine_notice_from_the_guardian(rig):
    t, b = rig

    def notice(device, state, key=GUARD_KEY, topic=None):
        body = {"id": "gd-1-%s" % state, "timestamp": pr.iso_ms(), "source": "guardian", "device_id": device, "state": state, "reason": "test"}
        payload = pr.seal(body, {"twin": key, "x": b"y"})
        b._on_message(None, None, type("M", (), {"topic": topic or "guardian/quarantine/" + device, "payload": payload})())

    notice("water-07", "quarantined")
    assert "WATER_07" in t.quarantined
    notice("water-07", "released")
    assert "WATER_07" not in t.quarantined
    notice("valve-03", "quarantined")
    command(b, "valve-03", "open", cid="q1")
    assert sent(b, "twin/actuator/")[-1][1]["reason"] == "quarantined" and t.cmds.qsize() == 0
    notice("valve-03", "released")
    command(b, "valve-03", "open", cid="q2")
    assert t.cmds.qsize() == 1
    notice("water-07", "quarantined", key=b"forged")
    assert "WATER_07" not in t.quarantined


def test_feedback_of_scenario_reset_is_a_plain_state_update(rig):
    t, b = rig
    t.valve_open["VALVE_03"] = True
    t.actuator_state["VALVE_03"] = "open"
    t.apply_reset()
    st = sent(b, "twin/actuator/valve-03")[-1][1]
    assert st["state"] == "closed" and "correlation_id" not in st and "result" not in st


def test_native_mode_is_untouched_when_the_profile_is_off(tmp_path):
    cfg = yaml.safe_load(open(os.path.join(TWIN, "city.yaml"), encoding="utf-8"))
    assert cfg["integration"]["enabled"] is False
    cfg["mqtt"]["enabled"] = False
    t = tw.Twin(cfg, str(tmp_path), fake=True, speed=1000)
    t.step_once()
    assert t.bridge is None and t.last


def test_recall_frees_a_busy_unit_and_is_acknowledged(rig):
    t, b = rig
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-a"}, cid="r1")
    t.step_once()
    assert t.units["CIVPRO_1"]["busy_until"] is not None and t.arrivals
    command(b, "civpro-1", "recall", cid="r2")
    t.step_once()
    assert t.units["CIVPRO_1"]["busy_until"] is None and not t.arrivals
    msgs = [x[1] for x in sent(b, "twin/unit/civpro-1")]
    ack = [m for m in msgs if m.get("correlation_id") == "r2"][0]
    assert ack["result"] == "done" and ack["status"] == "returning"
    assert msgs[-1]["status"] == "free"
    command(b, "civpro-1", "dispatch", {"zone_id": "zone-b"}, cid="r3")      # usable again at once
    t.step_once()
    assert t.units["CIVPRO_1"]["busy_until"] is not None
