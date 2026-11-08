import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.common.envelope import (
    EnvelopeError,
    parse_envelope,
    seal,
    seal_multi,
    sign,
    verify_ed25519,
    verify_hmac,
)

# Interface spec, annex B
VECTOR_KEY = b"test-secret-twin-brain"
VECTOR_BODY = (
    '{"correlation_id":"twin-000182","id":"twin-000182","sensor_id":"water-level-04",'
    '"source":"twin","timestamp":"2026-11-14T10:22:03.120Z","type":"water_level",'
    '"unit":"m","value":2.4,"zone_id":"port"}'
)
VECTOR_HMAC = "c97ecfbc4f3f1c82140c3bd78d804b3311af0bf051d5c98f025aef9f76b6771b"


def test_spec_test_vector():
    assert sign(VECTOR_BODY, VECTOR_KEY) == VECTOR_HMAC


def test_spec_test_vector_through_envelope():
    payload = json.dumps({"v": 1, "body": VECTOR_BODY, "hmac": VECTOR_HMAC})
    verify_hmac(parse_envelope(payload), VECTOR_KEY, "brain")


def test_seal_matches_reference_implementation():
    body = json.loads(VECTOR_BODY)
    env = json.loads(seal(dict(sorted(body.items())), VECTOR_KEY))
    assert env == {"v": 1, "body": VECTOR_BODY, "hmac": VECTOR_HMAC}


def test_hmacs_uses_own_entry():
    payload = seal_multi({"id": "a"}, {"brain": b"k1", "guardian": b"k2"})
    verify_hmac(parse_envelope(payload), b"k1", "brain")
    with pytest.raises(EnvelopeError) as e:
        verify_hmac(parse_envelope(payload), b"k2", "brain")
    assert e.value.code == "E_AUTH"


@pytest.mark.parametrize("payload", [
    "not json", "[1, 2]", '{"body": "{}", "hmac": "aa"}', '{"v": 2, "body": "{}", "hmac": "aa"}',
    '{"v": 1, "body": {"id": 1}, "hmac": "aa"}', '{"v": 1, "body": "{}", "hmacs": "aa"}',
])
def test_bad_envelopes_are_format_errors(payload):
    with pytest.raises(EnvelopeError) as e:
        parse_envelope(payload)
    assert e.value.code == "E_FORMAT"


def test_missing_hmac_is_auth_error():
    with pytest.raises(EnvelopeError) as e:
        verify_hmac(parse_envelope('{"v": 1, "body": "{}"}'), VECTOR_KEY, "brain")
    assert e.value.code == "E_AUTH"


def test_ed25519():
    import base64

    private = Ed25519PrivateKey.generate()
    body = '{"id":"guardian-1"}'
    sig = base64.b64encode(private.sign(body.encode())).decode()
    env = parse_envelope(json.dumps({"v": 1, "body": body, "sig": sig}))
    verify_ed25519(env, private.public_key(), "sig", "base64")
    tampered = parse_envelope(json.dumps({"v": 1, "body": body.replace("1", "2"), "sig": sig}))
    with pytest.raises(EnvelopeError) as e:
        verify_ed25519(tampered, private.public_key(), "sig", "base64")
    assert e.value.code == "E_AUTH"
