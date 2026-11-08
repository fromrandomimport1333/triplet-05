"""Brain-profile message envelope (integration kit, handover sections 4 and 5.3).

    {"v": 1, "body": "<JSON text>", "hmac": "<hex>"}                 single reader
    {"v": 1, "body": "<JSON text>", "hmacs": {"brain": hex, ...}}   several readers

HMAC-SHA256 over the exact UTF-8 bytes of ``body``. The key is the TEXT of the .key file with
surrounding whitespace stripped, used as UTF-8 bytes (never hex-decoded).
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_ID_LEN = 128


def iso_ms(dt: datetime | None = None) -> str:
    """ISO 8601 UTC with EXACTLY milliseconds, e.g. 2026-11-14T10:00:00.000Z."""
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def key_bytes(key_text: str) -> bytes:
    """Key text -> bytes used for HMAC (strip + UTF-8, never hex-decoded)."""
    return (key_text or "").strip().encode("utf-8")


def tag(key_text: str, body_text: str) -> str:
    return hmac.new(key_bytes(key_text), body_text.encode("utf-8"), hashlib.sha256).hexdigest()


def dumps_body(body: dict) -> str:
    """Serialise once (compact, UTF-8); this exact string is signed and sent."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def seal(body: dict, key_text: str) -> str:
    """Envelope for a single reader (the Brain): key ``hmac``."""
    text = dumps_body(body)
    return json.dumps({"v": 1, "body": text, "hmac": tag(key_text, text)})


def seal_multi(body: dict, keys: dict[str, str]) -> str:
    """Envelope for several readers: ``hmacs`` {reader: tag}."""
    text = dumps_body(body)
    return json.dumps({"v": 1, "body": text, "hmacs": {r: tag(k, text) for r, k in keys.items()}})


@dataclass
class Opened:
    ok: bool                      # signature verified
    reason: str                   # "ok" | "malformed" | "bad_hmac" | "no_tag"
    body: dict | None = None      # parsed body (also filled when the HMAC is bad, if parseable)
    body_text: str | None = None


def open_envelope(payload, key_text: str, my_tag: str = "guardian",
                  accept_single_hmac: bool = True) -> Opened:
    """Parse an envelope and verify OUR tag (hmacs[my_tag], else hmac when allowed).

    The body is parsed even when the signature fails so the caller can attribute a
    bad-HMAC message to a topic id / reading id for audit, but it is never trusted."""
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        env = json.loads(payload) if isinstance(payload, str) else payload
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Opened(False, "malformed")
    if not isinstance(env, dict) or env.get("v") != 1 or not isinstance(env.get("body"), str):
        return Opened(False, "malformed")
    text = env["body"]
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return Opened(False, "malformed", None, text)
    if not isinstance(body, dict):
        return Opened(False, "malformed", None, text)
    got = None
    if isinstance(env.get("hmacs"), dict):
        got = env["hmacs"].get(my_tag)
    if got is None and accept_single_hmac:
        got = env.get("hmac")
    if not isinstance(got, str):
        return Opened(False, "no_tag", body, text)
    if not key_text or not hmac.compare_digest(tag(key_text, text), got.lower()):
        return Opened(False, "bad_hmac", body, text)
    return Opened(True, "ok", body, text)


def has_tag(payload, my_tag: str) -> bool:
    """True if the envelope carries hmacs[my_tag] (used for twin/meta: verify only if tagged)."""
    try:
        env = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
        return isinstance(env, dict) and isinstance(env.get("hmacs"), dict) and my_tag in env["hmacs"]
    except (ValueError, TypeError):
        return False


class IdFactory:
    """Ids unique across restarts: gd-<run id>-<counter> (kit 5.9)."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = (run_id or secrets.token_hex(4)).lower()
        if not ID_PATTERN.match(self.run_id):
            raise ValueError("GUARDIAN_RUN_ID must match ^[a-z0-9][a-z0-9-]*$")
        self._n = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def prefix(self) -> str:
        return f"gd-{self.run_id}-"

    def next(self) -> str:
        with self._lock:
            return f"{self.prefix}{next(self._n):06d}"


def valid_id(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 64 and bool(ID_PATTERN.match(v))


def valid_msg_id(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= MAX_ID_LEN
