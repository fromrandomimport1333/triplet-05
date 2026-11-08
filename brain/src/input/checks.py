"""The 7 checks on every incoming message (team doc 01, section 4). Stop at the first failure.

    1 Envelope   valid JSON with v, body, hmac / hmacs           E_FORMAT
    2 Signature  HMAC-SHA256 (Twin, Guardian) or Ed25519 (Guardian) E_AUTH
    3 Freshness  timestamp within +-10 s of our clock             E_STALE
    4 Replay     id not seen in 60 s, seq increasing per device   E_REPLAY
    5 Format     required fields and types (pydantic)             E_FORMAT
    6 Known IDs  declared in twin/meta                            E_UNKNOWN_ID
    7 Range      value within the sensor's min / max              E_RANGE
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ..common.envelope import EnvelopeError, parse_envelope, verify_ed25519, verify_hmac
from ..common.models import EXTERNAL_MODELS
from .registry import CityRegistry
from .replay import ReplayCache, SeqTracker
from .topics import Match, TopicRouter

STATUS_KINDS = ("twin_status", "guardian_status")
DEVICE_FIELDS = ("sensor_id", "actuator_id", "unit_id", "device_id", "call_id")


class Rejected(Exception):
    def __init__(self, code: str, detail: str, message_id: str | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.message_id = message_id


@dataclass(slots=True)
class Accepted:
    kind: str
    source: str
    body: Any               # one of the models in EXTERNAL_MODELS
    vars: dict[str, str]
    retained: bool
    clock_offset_s: float | None   # our clock minus the sender's, when checked


def parse_timestamp(value: Any) -> float:
    """ISO 8601 with a timezone -> epoch seconds. ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("timestamp has no timezone (expected UTC, e.g. ...Z)")
    return ts.timestamp()


def _first_error(e: ValidationError) -> str:
    err = e.errors(include_url=False)[0]
    loc = ".".join(str(p) for p in err["loc"]) or "body"
    return f"{loc}: {err['msg']}" + (f" ({len(e.errors())} errors)" if e.error_count() > 1 else "")


class Checker:
    def __init__(
        self,
        *,
        router: TopicRouter,
        registry: CityRegistry,
        hmac_keys: dict[str, bytes],
        hmacs_self: str,
        checks: dict[str, Any],
        field_map: dict[str, dict[str, str]],
        verdict_aliases: dict[str, str],
        guardian_signature: dict[str, Any],
        ed25519_key: Any = None,
        clock: Callable[[], float],
    ):
        self.router = router
        self.registry = registry
        self.keys = hmac_keys
        self.hmacs_self = hmacs_self
        self.cfg = checks
        self.field_map = {k: v or {} for k, v in field_map.items()}
        self.verdict_aliases = verdict_aliases or {}
        self.guardian_sig = guardian_signature
        self.ed25519_key = ed25519_key
        self.clock = clock
        self.replay = ReplayCache(checks["replay_window_s"], checks["replay_cleanup_s"], clock)
        self.seq = SeqTracker()

    def run(self, topic: str, payload: bytes | str, retained: bool) -> Accepted:
        match = self.router.match(topic)
        if match is None:
            raise Rejected("E_FORMAT", f"no route for topic {topic}")
        if len(payload) > self.cfg["max_payload_bytes"]:
            raise Rejected("E_FORMAT", f"payload of {len(payload)} bytes over the limit")
        kind, source = match.route.kind, match.route.source

        # 1 envelope, 2 signature
        try:
            env = parse_envelope(payload)
            self._verify_signature(env, source)
        except EnvelopeError as e:
            raise Rejected(e.code, e.detail) from None

        try:
            raw = json.loads(env.body)
        except ValueError as e:
            raise Rejected("E_FORMAT", f"body is not valid JSON: {e}") from None
        if not isinstance(raw, dict):
            raise Rejected("E_FORMAT", "body is not a JSON object")
        raw = self._map_fields(kind, raw)
        message_id = raw.get("id") if isinstance(raw.get("id"), str) else None

        # Retained and Last Will messages are old by design (see config/input.yaml)
        relaxed = (retained and self.cfg["retained_skip_freshness"]) or (
            kind in STATUS_KINDS and self.cfg["status_skip_freshness"]
        )

        # 3 freshness
        offset = None
        try:
            sent_at = parse_timestamp(raw.get("timestamp"))
        except ValueError as e:
            raise Rejected("E_FORMAT", f"timestamp: {e}", message_id) from None
        if not relaxed:
            offset = self.clock() - sent_at
            window = self.cfg["clock_window_s"]
            if abs(offset) > window:
                raise Rejected(
                    "E_STALE", f"timestamp {offset:+.1f} s from our clock (limit +-{window} s)", message_id
                )

        # 4 replay
        if not message_id:
            raise Rejected("E_FORMAT", "id: missing or not a string")
        if relaxed:
            self.replay.add(source, message_id)
        else:
            if not self.replay.check_and_add(source, message_id):
                raise Rejected("E_REPLAY", f"id {message_id} already received", message_id)
            self._check_seq(source, kind, raw, message_id)

        # 5 format
        body = self._validate(kind, source, raw, match, message_id)

        # 6 known ids, 7 range
        self._check_ids_and_range(kind, body, message_id)
        return Accepted(kind, source, body, match.vars, retained, offset)

    # --- steps ------------------------------------------------------------------------

    def _verify_signature(self, env, source: str) -> None:
        if source == "guardian" and self.guardian_sig.get("mode") == "ed25519":
            verify_ed25519(
                env, self.ed25519_key, self.guardian_sig["sig_field"], self.guardian_sig["sig_encoding"]
            )
            return
        key = self.keys.get(source)
        if key is None:
            raise EnvelopeError("E_AUTH", f"no key configured for {source}")
        verify_hmac(env, key, self.hmacs_self)

    def _map_fields(self, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        for old, new in self.field_map.get(kind, {}).items():
            if old in raw and new not in raw:
                raw[new] = raw.pop(old)
        if kind == "verdict" and raw.get("verdict") in self.verdict_aliases:
            raw["verdict"] = self.verdict_aliases[raw["verdict"]]
        return raw

    def _check_seq(self, source: str, kind: str, raw: dict[str, Any], message_id: str) -> None:
        seq = raw.get("seq")
        if not self.cfg["check_seq"] or not isinstance(seq, int) or isinstance(seq, bool):
            return
        device = next((raw[f] for f in DEVICE_FIELDS if isinstance(raw.get(f), str)), kind)
        last = self.seq.check_and_update(source, device, seq)
        if last is not None:
            raise Rejected("E_REPLAY", f"seq {seq} not after {last} for {device}", message_id)

    def _validate(self, kind: str, source: str, raw: dict, match: Match, message_id: str) -> BaseModel:
        try:
            body = EXTERNAL_MODELS[kind].model_validate(raw)
        except ValidationError as e:
            raise Rejected("E_FORMAT", _first_error(e), message_id) from None
        if body.source != source:
            raise Rejected("E_FORMAT", f"source is {body.source!r} on a {source} topic", message_id)
        for name, value in match.vars.items():
            in_body = getattr(body, name, None)
            if in_body != value:
                raise Rejected(
                    "E_FORMAT", f"topic says {name}={value} but body says {in_body}", message_id
                )
        return body

    def _check_ids_and_range(self, kind: str, body: Any, message_id: str) -> None:
        reg = self.registry

        def unknown(detail: str) -> Rejected:
            return Rejected("E_UNKNOWN_ID", detail, message_id)

        if kind == "meta":
            problems = CityRegistry.problems(body)
            if problems:
                raise unknown("; ".join(problems[:5]))
            return
        if kind in STATUS_KINDS:
            return
        if not reg.loaded:
            raise unknown("no city description (twin/meta) received yet")

        if kind == "reading":
            decl = reg.sensors.get(body.sensor_id)
            if decl is None:
                raise unknown(f"sensor {body.sensor_id} not declared")
            if decl.zone_id != body.zone_id:
                raise unknown(f"sensor {body.sensor_id} is declared in zone {decl.zone_id}, not {body.zone_id}")
            if decl.type != body.type:
                raise unknown(f"sensor {body.sensor_id} is declared as {decl.type}, not {body.type}")
            if decl.unit != body.unit:
                raise Rejected("E_FORMAT", f"unit {body.unit!r}, expected {decl.unit!r}", message_id)
            if not decl.min <= body.value <= decl.max:
                raise Rejected(
                    "E_RANGE", f"value {body.value} outside [{decl.min}, {decl.max}]", message_id
                )
        elif kind == "call":
            if body.zone_id not in reg.zones:
                raise unknown(f"zone {body.zone_id} not declared")
        elif kind == "actuator":
            if body.actuator_id not in reg.actuators:
                raise unknown(f"actuator {body.actuator_id} not declared")
        elif kind == "unit":
            if body.unit_id not in reg.units:
                raise unknown(f"unit {body.unit_id} not declared")
            if body.zone_id is not None and body.zone_id not in reg.zones:
                raise unknown(f"zone {body.zone_id} not declared")
        elif kind == "verdict":
            if body.sensor_id not in reg.sensors:
                raise unknown(f"sensor {body.sensor_id} not declared")
        elif kind == "quarantine":
            if not reg.is_device(body.device_id):
                raise unknown(f"device {body.device_id} not declared")

