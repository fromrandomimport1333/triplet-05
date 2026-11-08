"""Event schema validation, Twin event signature (HMAC-SHA256) and Guardian's own
verdict signature (Ed25519). Also the schema-versioning policy check."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

EVENT_SCHEMA_VERSION = "1.0"
VERDICT_SCHEMA_VERSION = "1.0"
SUPPORTED_EVENT_MAJOR = 1

EVENT_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "schema_version": str,
    "event_id": str,
    "seq": int,
    "device_id": str,
    "sensor_type": str,
    "value": (int, float),
    "unit": str,
    "ts": str,
    "signature": str,
}


def canonical_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace, UTF-8) used for every hash/signature."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def check_schema_version(v: str) -> tuple[bool, str]:
    """Versioning policy: accept any 1.x (minor = additive only); reject other majors."""
    try:
        major = int(str(v).split(".")[0])
    except ValueError:
        return False, f"schema: unparseable schema_version {v!r}"
    if major != SUPPORTED_EVENT_MAJOR:
        return False, f"schema: unsupported major version {v} (supported {SUPPORTED_EVENT_MAJOR}.x)"
    return True, "ok"


def check_schema(evt: Any) -> tuple[bool, str]:
    """Structural validation of a Twin event (types, required fields, version, ts)."""
    if not isinstance(evt, dict):
        return False, "schema: event is not a JSON object"
    for k, t in EVENT_REQUIRED.items():
        if k not in evt:
            return False, f"schema: missing field '{k}'"
        if isinstance(evt[k], bool) or not isinstance(evt[k], t):
            return False, f"schema: field '{k}' has wrong type"
    ok, why = check_schema_version(evt["schema_version"])
    if not ok:
        return ok, why
    if not evt["event_id"] or not evt["device_id"]:
        return False, "schema: empty event_id/device_id"
    if evt["seq"] < 0:
        return False, "schema: negative seq"
    if isinstance(evt["value"], float) and not math.isfinite(evt["value"]):
        return False, "schema: non-finite value"
    try:
        parse_ts(evt["ts"])
    except Exception:
        return False, "schema: invalid ts (ISO-8601 with timezone required)"
    return True, "ok"


def _event_mac(evt: dict, key: str) -> str:
    body = {k: v for k, v in evt.items() if k != "signature"}
    return hmac.new(key.encode(), canonical_json(body).encode(), hashlib.sha256).hexdigest()


def sign_event(evt: dict, key: str) -> dict:
    """Used by Twin (and mocks/twin_mock.py): adds signature = HMAC-SHA256(key, canonical_json(evt - signature))."""
    out = dict(evt)
    out["signature"] = _event_mac(out, key)
    return out


def verify_event_signature(evt: dict, key: str) -> bool:
    """Constant-time verification of a Twin event signature."""
    if not key:
        return False
    return hmac.compare_digest(_event_mac(evt, key), str(evt.get("signature", "")))


class VerdictSigner:
    """Guardian's own signature on every verdict / control message (Ed25519).

    The signature covers canonical_json(message without 'sig'). Partners verify with the
    public key served at GET /.well-known/guardian-verdict-key.
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        self._sk = private_key
        pub = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.kid = hashlib.sha256(pub).hexdigest()[:16]

    @classmethod
    def generate(cls) -> "VerdictSigner":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_file_or_generate(cls, path: str) -> "VerdictSigner":
        if path and Path(path).is_file():
            sk = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
            if not isinstance(sk, Ed25519PrivateKey):
                raise ValueError("VERDICT_SIGNING_KEY_FILE must be an Ed25519 key")
            return cls(sk)
        signer = cls.generate()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(
                signer._sk.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        return signer

    def public_key_pem(self) -> str:
        return self._sk.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def sign(self, msg: dict) -> dict:
        body = {k: v for k, v in msg.items() if k != "sig"}
        sig = self._sk.sign(canonical_json(body).encode())
        return {**body, "sig": {"alg": "Ed25519", "kid": self.kid,
                                "value": base64.b64encode(sig).decode()}}

    @staticmethod
    def verify(msg: dict, public_key_pem: str) -> bool:
        try:
            pk = serialization.load_pem_public_key(public_key_pem.encode())
            assert isinstance(pk, Ed25519PublicKey)
            sig = base64.b64decode(msg["sig"]["value"])
            body = {k: v for k, v in msg.items() if k != "sig"}
            pk.verify(sig, canonical_json(body).encode())
            return True
        except (InvalidSignature, KeyError, TypeError, ValueError, AssertionError):
            return False
