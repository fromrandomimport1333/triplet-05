"""Unit tests for the Twin. No MQTT broker and no SUMO needed: the Twin runs in --fake mode and is stepped by hand."""
import os
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TWIN, "src"))
import twin as tw  # noqa: E402
from contract import sign, signature_ok  # noqa: E402

API, GKEY, TEST = {"X-API-Key": "change-me"}, {"X-API-Key": "guardian-twin-key"}, {"X-API-Key": "test-key"}


@pytest.fixture
def city(tmp_path):
    with open(os.path.join(TWIN, "city.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["mqtt"]["enabled"] = False
    cfg["schemas_dir"] = os.path.join(TWIN, "..", "docs", "schemas")
    cfg["api"].update(api_key="change-me", guardian_key="guardian-twin-key", test_key="test-key", hmac_key="test-hmac")
    t = tw.Twin(cfg, str(tmp_path), fake=True, speed=1000)
    return t, TestClient(tw.build_app(t))


def published(t, topic_part):
    return [e for e in t.log.read() if topic_part in e["topic"]]


def steps(t, n):
    for _ in range(n):
        t.step_once()


# ---------- signatures ----------
def test_readings_are_signed_and_tampering_is_detectable(city):
    t, _ = city
    steps(t, 2)
    msg = t.last["WATER_07"]
    assert signature_ok(msg, "test-hmac")
    assert not signature_ok(dict(msg, value=9.9), "test-hmac")
    assert msg["sig"] == sign(msg, "test-hmac")


def test_all_six_sensor_families_publish_on_their_topic(city):
    t, c = city
    steps(t, 2)
    c.post("/emergency_calls", headers=TEST, json={"call_type": "fire", "lonlat": [10.18, 36.80]})
    kinds = {e["payload"]["kind"] for e in t.log.read() if e["payload"].get("type") == "reading"}
    assert {"traffic_camera", "water_level", "rain", "air_quality", "power_load", "emergency_call"} <= kinds
    for e in t.log.read():
        p = e["payload"]
        if p.get("type") == "reading" and p["kind"] != "emergency_call":
            assert e["topic"] == "city/%s/%s/%s" % (p["zone_id"], p["kind"], p["sensor_id"])
            assert p["unit"] == tw.UNITS[p["kind"]]


def test_air_reading_carries_a_consistent_aqi(city):
    t, _ = city
    steps(t, 1)
    m = t.last["AIR_01"]
    assert m["aqi"] == tw.pm25_to_aqi(m["value"])
    assert tw.pm25_to_aqi(9.0) == 50 and tw.pm25_to_aqi(150) > 200 and tw.pm25_to_aqi(1000) == 500


# ---------- actuator validation ----------
@pytest.mark.parametrize("aid,args,fragment", [
    ("VALVE_03", {"state": "banana"}, "args.state"),
    ("TL_01", {"phase": 9}, "args.phase"),
    ("TL_01", {"phase": True}, "args.phase"),
    ("ALERT_01", {"level": "purple", "msg": "x"}, "args.level"),
    ("ALERT_01", {"level": "red", "msg": ""}, "args.msg"),
    ("ALERT_01", {"level": "red", "msg": "x" * 281}, "at most 280"),
    ("GRID_A", {"action": "explode"}, "args.action"),
    ("DISPATCH_01", {"type": "taxi", "zone_id": "zone_a"}, "args.type"),
    ("DISPATCH_01", {"type": "police", "zone_id": "mars"}, "args.zone_id"),
])
def test_bad_actuator_args_get_422_with_a_clear_error(city, aid, args, fragment):
    t, c = city
    r = c.post("/actuators/%s" % aid, headers=API, json={"cmd_id": "c1", "args": args})
    assert r.status_code == 422
    body = r.json()
    assert body["type"] == "error" and fragment in body["detail"]
    assert t.cmds.empty()


def test_command_body_is_checked_against_the_json_schema(city):
    _, c = city
    r = c.post("/actuators/VALVE_03", headers=API, json={"args": {"state": "open"}})
    assert r.status_code == 422 and "cmd_id" in r.json()["detail"] and r.json()["code"] == "SCHEMA_INVALID"
    r = c.post("/actuators/VALVE_03", headers=API, json={"cmd_id": "c1", "args": "open"})
    assert r.status_code == 422 and "args" in r.json()["detail"]


def test_valid_command_is_acked_then_fed_back(city):
    t, c = city
    r = c.post("/actuators/VALVE_03", headers=API, json={"cmd_id": "c1", "args": {"state": "open"}, "verdict_ref": "V-1"})
    assert r.status_code == 200
    ack = r.json()
    assert ack["type"] == "ack" and ack["accepted"] and ack["cmd_id"] == "c1" and ack["verdict_ref"] == "V-1"
    steps(t, 1)
    fb = published(t, "actuator/VALVE_03/state")[-1]["payload"]
    assert fb["ok"] and fb["state"] == "open" and fb["verdict_ref"] == "V-1"
    assert published(t, "actuator/VALVE_03/ack")


def test_tool_call_resolves_zone_to_device(city):
    t, c = city
    r = c.post("/tools/broadcast_alert", headers=API, json={"cmd_id": "a1", "args": {"zone_id": "zone_b", "level": "orange", "text": "Smog"}})
    assert r.status_code == 200 and r.json()["actuator_id"] == "ALERT_02"
    r = c.post("/tools/set_water_valve", headers=API, json={"cmd_id": "v1", "args": {"zone_id": "zone_b", "state": "open"}})
    assert r.status_code == 422 and "no water_valve" in r.json()["detail"]
    assert c.post("/tools/fly", headers=API, json={"cmd_id": "x", "args": {}}).status_code == 404


# ---------- new actuators ----------
def test_power_grid_shed_lowers_that_zones_load(city):
    t, c = city
    steps(t, 3)
    before = t.last["POWER_02"]["value"]
    c.post("/actuators/GRID_A", headers=API, json={"cmd_id": "g1", "args": {"action": "shed"}})
    steps(t, 2)
    after = t.last["POWER_02"]["value"]
    assert after < before * 0.8
    assert published(t, "actuator/GRID_A/state")[-1]["payload"]["state"] == "shed"


def test_dispatch_reports_dispatched_then_arrived(city):
    t, c = city
    r = c.post("/tools/dispatch_emergency", headers=API, json={"cmd_id": "d1", "args": {"type": "ambulance", "zone_id": "zone_a"}})
    assert r.status_code == 200
    steps(t, 1)
    fb = published(t, "actuator/DISPATCH_01/state")
    assert fb[-1]["payload"]["state"] == "dispatched" and fb[-1]["payload"]["eta_s"] > 0
    eta = fb[-1]["payload"]["eta_s"]
    steps(t, int(eta) + 2)
    states = [e["payload"]["state"] for e in published(t, "actuator/DISPATCH_01/state")]
    assert states[-1] == "arrived"


def test_dispatch_with_no_free_unit_fails_cleanly(city):
    t, c = city
    c.post("/tools/dispatch_emergency", headers=API, json={"cmd_id": "d1", "args": {"type": "civil_protection", "zone_id": "zone_a"}})
    c.post("/tools/dispatch_emergency", headers=API, json={"cmd_id": "d2", "args": {"type": "civil_protection", "zone_id": "zone_b"}})
    steps(t, 1)
    last = published(t, "actuator/DISPATCH_01/state")[-1]["payload"]
    assert last["cmd_id"] == "d2" and last["ok"] is False and "no civil_protection unit" in last["detail"]


def test_quarantine_needs_the_guardian_key_and_marks_readings(city):
    t, c = city
    assert c.post("/quarantine/AIR_01", headers=API).status_code == 403
    assert c.post("/actuators/QUARANTINE", headers=API, json={"cmd_id": "q", "args": {"sensor_id": "AIR_01"}}).status_code == 403
    assert c.post("/actuators/VALVE_03", headers=GKEY, json={"cmd_id": "v", "args": {"state": "open"}}).status_code == 403
    assert c.post("/quarantine/AIR_01", headers=GKEY).status_code == 200
    steps(t, 2)
    assert t.last["AIR_01"]["quarantined"] is True and signature_ok(t.last["AIR_01"], "test-hmac")
    assert c.get("/quarantine").json() == ["AIR_01"]
    assert c.delete("/quarantine/AIR_01", headers=GKEY).status_code == 200
    steps(t, 2)
    assert "quarantined" not in t.last["AIR_01"]


# ---------- faults ----------
def test_drift_adds_a_growing_offset_and_keeps_truth(city):
    t, c = city
    steps(t, 2)
    c.post("/faults", headers=TEST, json={"sensor_id": "AIR_02", "kind": "drift", "value": 0.5})
    steps(t, 20)
    m = t.last["AIR_02"]
    assert m["fault"]["kind"] == "drift"
    assert m["value"] - t.truth["AIR_02"] == pytest.approx(0.5 * 19, abs=1.0)


def test_replay_republishes_old_signed_messages(city):
    t, c = city
    steps(t, 10)
    newest_seq = t.last["AIR_04"]["seq"]
    c.post("/faults", headers=TEST, json={"sensor_id": "AIR_04", "kind": "replay"})
    steps(t, 3)
    m = t.last["AIR_04"]
    assert m["seq"] < newest_seq and signature_ok(m, "test-hmac") and "fault" not in m
    c.delete("/faults/AIR_04", headers=TEST)
    steps(t, 1)
    assert t.last["AIR_04"]["seq"] > newest_seq + 2      # the device kept counting during the attack


def test_flood_network_publishes_many_messages(city):
    t, c = city
    steps(t, 1)
    n0 = len(published(t, "/AIR_03"))
    c.post("/faults", headers=TEST, json={"sensor_id": "AIR_03", "kind": "flood_network", "value": 25})
    steps(t, 2)
    assert len(published(t, "/AIR_03")) - n0 >= 50
    assert c.post("/faults", headers=TEST, json={"sensor_id": "AIR_03", "kind": "flood_network", "value": 5000}).status_code == 422


def test_camera_fault_changes_speed_and_stopped_consistently(city):
    t, c = city
    steps(t, 2)
    c.post("/faults", headers=TEST, json={"sensor_id": "CAM_01", "kind": "spoof", "value": 29})
    steps(t, 1)
    m = t.last["CAM_01"]
    assert m["value"] == 29 and m["density_pct"] > 80 and m["mean_speed_kmh"] < 10 and m["halted_vehicles"] > 0
    assert t.truth_fields["CAM_01"]["mean_speed_kmh"] > 30          # the real road is still flowing


def test_jam_condition_slows_the_zone_cameras(city):
    t, c = city
    c.post("/scenario/conditions", headers=TEST, json={"zone_id": "zone_b", "jam": 1.0})
    steps(t, 90)
    jammed = t.last["CAM_02"]          # zone_b
    free = t.last["CAM_01"]            # zone_a
    assert jammed["mean_speed_kmh"] < 10 and jammed["density_pct"] > 80
    assert free["mean_speed_kmh"] > 30


def test_fault_kinds_are_validated(city):
    _, c = city
    assert c.post("/faults", headers=TEST, json={"sensor_id": "AIR_01", "kind": "drift"}).status_code == 422
    assert c.post("/faults", headers=TEST, json={"sensor_id": "EMERGENCY_01", "kind": "replay"}).status_code == 422
    assert c.post("/faults", headers=TEST, json={"sensor_id": "AIR_01", "kind": "melt"}).status_code == 422


def test_silent_stuck_valve_reports_open_but_does_not_drain(city):
    t, c = city
    c.post("/faults", headers=TEST, json={"sensor_id": "VALVE_03", "kind": "actuator_stuck", "value": 1})
    c.post("/actuators/VALVE_03", headers=API, json={"cmd_id": "c1", "args": {"state": "open"}})
    steps(t, 1)
    fb = published(t, "actuator/VALVE_03/state")[-1]["payload"]
    assert fb["ok"] and fb["state"] == "open"
    assert not t.valve_open.get("VALVE_03")


def test_env_overrides_keys():
    from contract import apply_env
    cfg = apply_env({"api": {"api_key": "a"}, "mqtt": {"port": 1883}}, environ={"TWIN_API_KEY": "secret", "MQTT_PORT": "1884"})
    assert cfg["api"]["api_key"] == "secret" and cfg["mqtt"]["port"] == 1884


# ---------- key permissions ----------
def test_each_key_can_only_do_its_own_job(city):
    _, c = city
    fault = {"sensor_id": "AIR_01", "kind": "stuck"}
    assert c.post("/faults", headers=API, json=fault).status_code == 403          # Brain may not inject faults
    assert c.post("/faults", headers=GKEY, json=fault).status_code == 403         # nor may the Guardian
    assert c.get("/truth/AIR_01", headers=API).status_code == 403                 # nobody but tests reads ground truth
    assert c.post("/faults", headers=TEST, json=fault).status_code == 200
    valve = {"cmd_id": "c1", "args": {"state": "open"}}
    assert c.post("/actuators/VALVE_03", headers=TEST, json=valve).status_code == 403   # tests do not drive actuators
    assert c.post("/actuators/VALVE_03", headers={"X-API-Key": "wrong"}, json=valve).status_code == 401
    assert c.post("/actuators/VALVE_03", headers=API, json=valve).status_code == 200
    assert c.get("/actuators/VALVE_03", headers=TEST).status_code == 200


def test_reset_restores_the_baseline(city):
    t, c = city
    c.post("/actuators/VALVE_03", headers=API, json={"cmd_id": "c1", "args": {"state": "open"}})
    c.post("/actuators/GRID_A", headers=API, json={"cmd_id": "c2", "args": {"action": "shed"}})
    c.post("/scenario/conditions", headers=TEST, json={"rain_mm_h": 60})
    steps(t, 5)
    c.post("/scenario/reset", headers=TEST)
    steps(t, 2)
    assert t.actuator_state["VALVE_03"] == "closed" and t.actuator_state["GRID_A"] == "normal"
    assert t.rain_mm_h == 0 and abs(t.last["WATER_07"]["value"] - 0.4) < 0.05
    assert published(t, "actuator/VALVE_03/state")[-1]["payload"]["cmd_id"] == "scenario-reset"


def test_contract_endpoint_describes_the_twin(city):
    _, c = city
    body = c.get("/contract").json()
    assert body["contract_version"] == "1.0"
    assert "POST /actuators/{aid}" in body["endpoints"] and "GET /contract" in body["endpoints"]
    assert {"reading", "command", "verdict", "decision"} <= set(body["schemas"])
    assert "city/<zone>/<kind>/<sensor_id>" in body["mqtt"]["publishes"]
