import time

import jwt
import pytest

from src.core.security import (AuthError, ForbiddenError, JwtVerifier, check_client_cert,
                               issue_tool_token)

KEYS = {"brain": "b" * 32, "admin": "a" * 32, "twin": "t" * 32, "ui": "u" * 32}


def v():
    return JwtVerifier(KEYS, max_ttl_s=120)


def test_valid_token_for_its_tool():
    c = v().verify(issue_tool_token(KEYS["brain"], "brain", "get_trust_verdict"), "get_trust_verdict")
    assert c["role"] == "brain"


def test_brain_cannot_call_release_device():
    with pytest.raises(ForbiddenError):
        v().verify(issue_tool_token(KEYS["brain"], "brain", "release_device"), "release_device")


def test_token_scoped_to_one_tool():
    with pytest.raises(ForbiddenError):
        v().verify(issue_tool_token(KEYS["admin"], "admin", "health"), "release_device")


def test_brain_key_cannot_forge_admin():
    forged = issue_tool_token(KEYS["brain"], "admin", "release_device")  # kid=admin, brain's key
    with pytest.raises(AuthError):
        v().verify(forged, "release_device")


def test_iss_kid_mismatch_rejected():
    now = int(time.time())
    t = jwt.encode({"iss": "admin", "aud": "guardian", "tool": "health", "iat": now, "exp": now + 30,
                    "jti": "x"}, KEYS["brain"], algorithm="HS256", headers={"kid": "brain"})
    with pytest.raises(AuthError):
        v().verify(t, "health")


def test_expired_long_lived_reused_and_alg_none():
    ver = v()
    with pytest.raises(AuthError):
        ver.verify(issue_tool_token(KEYS["brain"], "brain", "health", ttl_s=-5), "health")
    with pytest.raises(AuthError):
        ver.verify(issue_tool_token(KEYS["brain"], "brain", "health", ttl_s=3600), "health")
    t = issue_tool_token(KEYS["brain"], "brain", "health")
    ver.verify(t, "health")
    with pytest.raises(AuthError):
        ver.verify(t, "health")  # jti replay
    none_tok = jwt.encode({"iss": "admin", "tool": "release_device"}, None, algorithm="none",
                          headers={"kid": "admin"})
    with pytest.raises(AuthError):
        ver.verify(none_tok, "release_device")
    with pytest.raises(AuthError):
        ver.verify(None, "health")


def test_wrong_audience():
    t = issue_tool_token(KEYS["brain"], "brain", "health", audience="brain")
    with pytest.raises(AuthError):
        v().verify(t, "health")


def test_cert_must_match_token_issuer():
    t = issue_tool_token(KEYS["admin"], "admin", "release_device")
    with pytest.raises(AuthError):
        v().verify(t, "release_device", cert_issuer="brain")  # stolen admin token on brain's cert


def test_client_cert_cn_allowlist():
    allowed = {"brain.triplet": "brain"}
    cert = {"subject": ((("organizationName", "x"),), (("commonName", "brain.triplet"),))}
    assert check_client_cert(cert, allowed) == "brain"
    with pytest.raises(AuthError):
        check_client_cert(None, allowed)
    with pytest.raises(AuthError):
        check_client_cert({"subject": ((("commonName", "evil"),),)}, allowed)
