"""Envelope and HMAC (spec section 4.2, SEC-7 to SEC-11, annex B).

Every MQTT payload between layers is {"v": 1, "body": "<JSON text>", "hmac": "<hex>"}
or, for messages read by several layers, {"v": 1, "body": ..., "hmacs": {"brain": ..., ...}}.
The HMAC is computed over the exact body text, so JSON formatting never matters.
The input team verifies; the output team seals with the same functions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

ENVELOPE_VERSION = 1


class EnvelopeError(Exception):
    """Raised with an error code (E_FORMAT, E_AUTH) when an envelope is refused."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def body_text(body: dict[str, Any]) -> str:
    """Compact JSON text of a body, as in the reference implementation."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def sign(text: str, key: bytes) -> str:
    """HMAC-SHA256 of the UTF-8 bytes of `text`, 64 lowercase hex characters (SEC-8)."""
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def seal(body: dict[str, Any], key: bytes) -> str:
    """Envelope for a single receiver."""
    text = body_text(body)
    return json.dumps({"v": ENVELOPE_VERSION, "body": text, "hmac": sign(text, key)})


def seal_multi(body: dict[str, Any], keys: dict[str, bytes]) -> str:
    """Envelope for several receivers: one HMAC per receiving layer (SEC-10)."""
    text = body_text(body)
    hmacs = {layer: sign(text, key) for layer, key in keys.items()}
    return json.dumps({"v": ENVELOPE_VERSION, "body": text, "hmacs": hmacs})



def seal_for(body: dict[str, Any], keys: dict[str, bytes] | None) -> str:
    """Envelope for the receiving layers, or the plain body text when sealing is off (tests, debug)."""
    return seal_multi(body, keys) if keys else body_text(body)

@dataclass(slots=True)
class Envelope:
    body: str
    hmac: str | None
    hmacs: dict[str, str] | None
    raw: dict[str, Any]


def parse_envelope(payload: bytes | str) -> Envelope:
    """Check 1: valid JSON with v, body and hmac (or hmacs). Raises E_FORMAT."""
    try:
        env = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as e:
        raise EnvelopeError("E_FORMAT", f"envelope is not valid JSON: {e}") from None
    if not isinstance(env, dict):
        raise EnvelopeError("E_FORMAT", "envelope is not a JSON object")
    if env.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("E_FORMAT", f"unsupported envelope version {env.get('v')!r}")
    body = env.get("body")
    if not isinstance(body, str):
        raise EnvelopeError("E_FORMAT", "envelope `body` must be a JSON text string")
    tag = env.get("hmac")
    tags = env.get("hmacs")
    if tag is not None and not isinstance(tag, str):
        raise EnvelopeError("E_FORMAT", "envelope `hmac` must be a string")
    if tags is not None and not (
        isinstance(tags, dict) and all(isinstance(v, str) for v in tags.values())
    ):
        raise EnvelopeError("E_FORMAT", "envelope `hmacs` must map layer names to strings")
    return Envelope(body=body, hmac=tag, hmacs=tags, raw=env)


def verify_hmac(env: Envelope, key: bytes, self_name: str) -> None:
    """Check 2 (HMAC): `hmac`, or `hmacs[self_name]`, must match. Raises E_AUTH."""
    tag = env.hmac
    if tag is None and env.hmacs is not None:
        tag = env.hmacs.get(self_name)
    if not tag:
        raise EnvelopeError("E_AUTH", "HMAC missing")
    if not hmac.compare_digest(sign(env.body, key), tag.lower()):
        raise EnvelopeError("E_AUTH", "HMAC does not match the body")


def verify_ed25519(env: Envelope, public_key: Any, sig_field: str, encoding: str) -> None:
    """Check 2 (Ed25519, Guardian alignment): signature over the body text. Raises E_AUTH."""
    from cryptography.exceptions import InvalidSignature

    sig = env.raw.get(sig_field)
    if not isinstance(sig, str) or not sig:
        raise EnvelopeError("E_AUTH", f"signature `{sig_field}` missing")
    try:
        raw = bytes.fromhex(sig) if encoding == "hex" else base64.b64decode(sig, validate=True)
    except (ValueError, binascii.Error):
        raise EnvelopeError("E_AUTH", "signature is not valid " + encoding) from None
    try:
        public_key.verify(raw, env.body.encode("utf-8"))
    except InvalidSignature:
        raise EnvelopeError("E_AUTH", "Ed25519 signature does not match the body") from None


def load_ed25519_public_key(text: str) -> Any:
    """Accept a PEM public key or 64 hex characters (raw 32-byte key)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    text = text.strip()
    if text.startswith("-----BEGIN"):
        key = load_pem_public_key(text.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("public key is not Ed25519")
        return key
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(text))
