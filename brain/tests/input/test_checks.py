"""One valid and at least one invalid case per check (team doc 01, section 6)."""

import json

import pytest

from src.common.envelope import seal

from .conftest import GUARDIAN_KEY, META, TWIN_KEY


def readings(h):
    return h.bus.on("brain/internal/reading")


# 1 envelope ---------------------------------------------------------------------------

def test_valid_envelope_is_accepted(ready):
    ready.send_reading()
    assert len(readings(ready)) == 1
    assert ready.errors() == []


@pytest.mark.parametrize("payload", ["{oops", '{"v":1,"hmac":"aa"}', '"just a string"'])
def test_bad_envelope_is_rejected(ready, payload):
    ready.service.handle("city/port/sensor/water_level/water-level-04", payload)
    assert ready.last_error_code() == "E_FORMAT"
    assert readings(ready) == []


# 2 signature --------------------------------------------------------------------------

def test_wrong_hmac_is_rejected(ready):   # IT-3
    body = ready.reading()
    ready.send("city/port/sensor/water_level/water-level-04", body, key=b"wrong-key")
    assert ready.last_error_code() == "E_AUTH"
    assert readings(ready) == []


def test_body_changed_after_signing_is_rejected(ready):
    env = json.loads(seal(ready.reading(), TWIN_KEY))
    env["body"] = env["body"].replace("2.4", "3.9")
    ready.service.handle("city/port/sensor/water_level/water-level-04", json.dumps(env))
    assert ready.last_error_code() == "E_AUTH"


def test_hmacs_for_several_receivers_is_accepted(ready):
    body = ready.reading()
    ready.send("city/port/sensor/water_level/water-level-04", body, multi=True)
    assert len(readings(ready)) == 1


def test_twin_key_is_not_accepted_on_guardian_topic(ready):
    verdict = ready.guardian(sensor_id="water-level-04", verdict="trusted", reason="ok")
    ready.send("guardian/verdict/water-level-04", verdict, key=TWIN_KEY)
    assert ready.last_error_code() == "E_AUTH"
    ready.send("guardian/verdict/water-level-04", verdict, key=GUARDIAN_KEY)
    assert len(ready.bus.on("brain/internal/trust/")) == 1


# 3 freshness --------------------------------------------------------------------------

@pytest.mark.parametrize("offset, ok", [(0, True), (-9.5, True), (9.5, True), (-30, False), (11, False)])
def test_freshness_window(ready, offset, ok):   # IT-4 with -30
    body = ready.reading(timestamp=ready.clock.iso(offset))
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert (len(readings(ready)) == 1) is ok
    assert ready.last_error_code() == (None if ok else "E_STALE")


def test_timestamp_without_timezone_is_format_error(ready):
    body = ready.reading(timestamp="2026-11-14T10:22:00.000")
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert ready.last_error_code() == "E_FORMAT"


def test_retained_message_skips_freshness(h):
    old = {**META, "timestamp": h.clock.iso(-3600)}
    h.send("twin/meta", old, retained=True)
    assert h.errors() == []
    assert len(h.bus.on("brain/internal/meta")) == 1


# 4 replay -----------------------------------------------------------------------------

def test_same_message_twice_is_replay(ready):   # IT-5
    body = ready.reading()
    ready.send("city/port/sensor/water_level/water-level-04", body)
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert len(readings(ready)) == 1
    assert ready.last_error_code() == "E_REPLAY"


def test_id_forgotten_after_window(ready):
    body = ready.reading()
    ready.send("city/port/sensor/water_level/water-level-04", body)
    ready.clock.t += 61
    body["timestamp"] = ready.clock.iso()
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert len(readings(ready)) == 2


def test_seq_must_increase(ready):
    ready.send_reading(seq=5)
    ready.send_reading(seq=6)
    ready.send_reading(seq=6)
    assert len(readings(ready)) == 2
    assert ready.last_error_code() == "E_REPLAY"


def test_seq_restarts_when_twin_comes_back_online(ready):
    ready.send_reading(seq=50)
    ready.send("twin/status", ready.body(status="offline"))
    ready.send("twin/status", ready.body(status="online"))
    ready.send_reading(seq=1)
    assert len(readings(ready)) == 2


# 5 format -----------------------------------------------------------------------------

def test_missing_value_is_format_error(ready):
    body = ready.reading()
    del body["value"]
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert ready.last_error_code() == "E_FORMAT"


@pytest.mark.parametrize("value", ["2.4", True, None, [2.4]])
def test_value_must_be_a_number(ready, value):
    ready.send_reading(value=value)
    assert ready.last_error_code() == "E_FORMAT"


def test_unknown_extra_fields_are_ignored(ready):   # MSG-5
    ready.send_reading(battery=0.8, firmware="1.2")
    assert len(readings(ready)) == 1


def test_topic_and_body_must_agree(ready):
    body = ready.reading()
    ready.send("city/z3/sensor/water_level/water-level-04", body)
    assert ready.last_error_code() == "E_FORMAT"


def test_wrong_source_is_format_error(ready):
    body = ready.reading(source="guardian")
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert ready.last_error_code() == "E_FORMAT"


def test_field_mapping_from_config(ready):
    ready.service.checker.field_map["reading"] = {"device_id": "sensor_id"}
    body = ready.reading()
    body["device_id"] = body.pop("sensor_id")
    ready.send("city/port/sensor/water_level/water-level-04", body)
    assert readings(ready)[0]["sensor_id"] == "water-level-04"


# 6 known ids --------------------------------------------------------------------------

def test_undeclared_sensor_is_unknown_id(ready):   # IT-6
    ready.send_reading(sensor="water-level-99")
    assert ready.last_error_code() == "E_UNKNOWN_ID"


def test_reading_before_city_description_is_unknown_id(h):
    h.send_reading()
    assert h.last_error_code() == "E_UNKNOWN_ID"


def test_sensor_in_wrong_zone_is_unknown_id(ready):
    ready.send_reading(zone="z3")
    assert ready.last_error_code() == "E_UNKNOWN_ID"


def test_undeclared_actuator_and_unit(ready):
    ready.send("twin/actuator/valve-x9", ready.body(actuator_id="valve-x9", result="done", state="open"))
    assert ready.last_error_code() == "E_UNKNOWN_ID"
    ready.send("twin/unit/ambulance-2", ready.body(unit_id="ambulance-2", status="en_route", zone_id="z3"))
    assert ready.bus.on("brain/internal/ack")[0]["status"] == "en_route"


def test_city_description_with_dangling_reference_is_rejected(h):
    bad = {**META, "timestamp": h.clock.iso(),
           "sensors": [{**META["sensors"][0], "zone_id": "nowhere"}]}
    h.send("twin/meta", bad)
    assert h.last_error_code() == "E_UNKNOWN_ID"
    assert not h.service.registry.loaded


# 7 range ------------------------------------------------------------------------------

@pytest.mark.parametrize("value, ok", [(0.0, True), (4.0, True), (2, True), (-5.0, False), (4.01, False)])
def test_range(ready, value, ok):
    ready.send_reading(value=value)
    assert (len(readings(ready)) == 1) is ok
    if not ok:
        err = ready.errors()[-1]
        assert err["code"] == "E_RANGE"
        assert "outside [0.0, 4.0]" in err["detail"]


def test_wrong_unit_is_format_error(ready):
    ready.send_reading(unit="cm")
    assert ready.last_error_code() == "E_FORMAT"
