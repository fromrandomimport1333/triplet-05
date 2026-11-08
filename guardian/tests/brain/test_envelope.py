"""A1 / A2: key text handling, the kit's HMAC test vector, millisecond timestamps, envelopes."""
import json
import re
from datetime import datetime, timezone

from src.adapter.envelope import IdFactory, iso_ms, open_envelope, seal, seal_multi, tag
from src.config import read_key

KIT_KEY = "test-key-do-not-use"
KIT_BODY = ('{"id":"tw-test-0001","timestamp":"2026-11-14T10:00:00.000Z","source":"twin",'
            '"sensor_id":"water-01","zone_id":"zone-01","type":"water_level","value":0.42,'
            '"unit":"m","origin":"simulated","seq":1}')
KIT_HMAC = "503688fdc8bfb862d0617d77924a992ca83840855b3fd987df36aeeb2e12678d"


def test_kit_test_vector():
    assert tag(KIT_KEY, KIT_BODY) == KIT_HMAC


def test_read_key_strips_whitespace_file_and_env(tmp_path):
    f = tmp_path / "k.key"
    f.write_text(KIT_KEY + "\n", encoding="utf-8")            # trailing newline must NOT matter
    assert read_key(str(f)) == KIT_KEY
    f.write_text("  " + KIT_KEY + " \r\n", encoding="utf-8")
    assert read_key(str(f)) == KIT_KEY
    assert read_key("", " " + KIT_KEY + "\n") == KIT_KEY
    assert tag(read_key(str(f)), KIT_BODY) == KIT_HMAC        # never hex-decoded


def test_key_is_text_not_hex_decoded():
    hexkey = "00" * 32
    assert tag(hexkey, "x") != tag(bytes.fromhex(hexkey).decode("latin-1"), "x")


def test_iso_ms_exactly_milliseconds():
    s = iso_ms(datetime(2026, 11, 14, 10, 0, 0, 123456, tzinfo=timezone.utc))
    assert s == "2026-11-14T10:00:00.123Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", iso_ms())
    assert iso_ms(datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)).endswith(":00.000Z")


def test_seal_signs_exact_body_text_and_opens():
    env = json.loads(seal({"a": 1, "é": "x"}, "k"))
    assert env["v"] == 1 and isinstance(env["body"], str) and env["hmac"] == tag("k", env["body"])
    op = open_envelope(json.dumps(env), "k")
    assert op.ok and op.body == {"a": 1, "é": "x"}


def test_open_hmacs_uses_our_tag_only():
    text = seal_multi({"x": 1}, {"brain": "kb", "guardian": "kg"})
    assert open_envelope(text, "kg", "guardian").ok
    assert open_envelope(text, "kb", "guardian").reason == "bad_hmac"
    assert open_envelope(seal_multi({"x": 1}, {"brain": "kb"}), "kg", "guardian").reason == "no_tag"


def test_open_malformed_and_tampered():
    assert open_envelope(b"\x00\xffnot json", "k").reason == "malformed"
    assert open_envelope('{"v":2,"body":"{}","hmac":"00"}', "k").reason == "malformed"
    assert open_envelope('{"v":1,"body":"[1]","hmac":"00"}', "k").reason == "malformed"
    env = json.loads(seal({"value": 0.4}, "k"))
    env["body"] = env["body"].replace("0.4", "1.8")
    op = open_envelope(json.dumps(env), "k")
    assert not op.ok and op.reason == "bad_hmac" and op.body == {"value": 1.8}
    assert open_envelope(seal({"a": 1}, "k"), "").reason == "bad_hmac"   # no key configured


def test_ids_prefixed_with_run_id_and_unique():
    f = IdFactory("r9")
    a, b = f.next(), f.next()
    assert a.startswith("gd-r9-") and a != b and len(a) <= 128
    assert IdFactory().run_id != IdFactory().run_id
