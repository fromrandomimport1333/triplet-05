"""City Brain protocol profile, part 1: the envelope. Pure functions, no MQTT, no Twin state.

Every MQTT payload between the layers is  {"v": 1, "body": "<JSON text>", "hmac": "<hex>"}  or, when several layers
read it,  {"v": 1, "body": ..., "hmacs": {"brain": "<hex>", "guardian": "<hex>"}}.
The HMAC-SHA256 is computed over the exact UTF-8 bytes of `body`, with the key file's text (stripped, NOT hex-decoded)
as key. Serialise once, sign that string, send that same string.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

ENVELOPE_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ProtocolError(Exception):
    """A partner sent something we refuse. `code` follows the Brain's vocabulary (E_FORMAT, E_AUTH, E_STALE, ...)."""

    def __init__(self, code, detail):
        super().__init__("%s: %s" % (code, detail))
        self.code, self.detail = code, detail


def normalize_id(native):
    """CAM_01 -> cam-01. The partners accept only [a-z0-9][a-z0-9-]*."""
    return re.sub(r"[^a-z0-9-]+", "-", str(native).lower()).strip("-")


def iso_ms(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(text):
    if not isinstance(text, str):
        raise ValueError("timestamp must be a string")
    ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return ts.timestamp()


def read_key(path):
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        raise ValueError("key file %s is empty" % path)
    return text.encode("utf-8")


def body_text(body):
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def tag(text, key):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def seal(body, keys):
    """Envelope for the layers in `keys` ({"brain": key, "guardian": key}); one tag per layer.
    With a single layer the field is called `hmac`."""
    text = body_text(body)
    if len(keys) == 1:
        return json.dumps({"v": ENVELOPE_VERSION, "body": text, "hmac": tag(text, next(iter(keys.values())))})
    return json.dumps({"v": ENVELOPE_VERSION, "body": text, "hmacs": {l: tag(text, k) for l, k in keys.items()}})


def open_envelope(payload, key, self_name):
    """Verify and decode an incoming envelope. Returns the body dict. Raises ProtocolError (E_FORMAT or E_AUTH)."""
    try:
        env = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError("E_FORMAT", "envelope is not valid JSON: %s" % e)
    if not isinstance(env, dict) or env.get("v") != ENVELOPE_VERSION:
        raise ProtocolError("E_FORMAT", "envelope must be an object with v=%d" % ENVELOPE_VERSION)
    text = env.get("body")
    if not isinstance(text, str):
        raise ProtocolError("E_FORMAT", "envelope.body must be a JSON text string, not a nested object")
    given = env.get("hmac")
    if given is None and isinstance(env.get("hmacs"), dict):
        given = env["hmacs"].get(self_name)
    if not isinstance(given, str) or not given:
        raise ProtocolError("E_AUTH", "no HMAC for %s in the envelope (hmac or hmacs.%s)" % (self_name, self_name))
    if not hmac.compare_digest(tag(text, key), given.lower()):
        raise ProtocolError("E_AUTH", "HMAC does not match the body text (wrong key, or computed over re-serialised JSON)")
    try:
        body = json.loads(text)
    except ValueError as e:
        raise ProtocolError("E_FORMAT", "body is not valid JSON: %s" % e)
    if not isinstance(body, dict):
        raise ProtocolError("E_FORMAT", "body must be a JSON object")
    return body


def check_fresh(body, now, window_s):
    """The partners require a timestamp within +-window_s of the receiver's clock."""
    try:
        sent = parse_ts(body.get("timestamp"))
    except ValueError as e:
        raise ProtocolError("E_FORMAT", "timestamp: %s" % e)
    if abs(now - sent) > window_s:
        raise ProtocolError("E_STALE", "timestamp is %+.1f s from our clock (limit +-%s s)" % (now - sent, window_s))


def require_fields(body, fields):
    for name, kind in fields.items():
        if name not in body:
            raise ProtocolError("E_FORMAT", "%s: missing" % name)
        if not isinstance(body[name], kind):
            raise ProtocolError("E_FORMAT", "%s: expected %s, got %s" % (name, kind.__name__, type(body[name]).__name__))
