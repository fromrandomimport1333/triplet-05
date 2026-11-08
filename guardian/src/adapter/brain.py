"""Brain protocol profile (city-brain integration kit). Enabled by BRAIN_PROTOCOL_ENABLED=true.

The native protocol (INTERFACE.md sections 1-7) is untouched: this adapter translates the
kit's messages into native events, runs them through the SAME detection pipeline,
quarantine manager and hash-chained audit log, and translates the results back.

  in : twin/meta, city/{zone}/sensor/{type}/{sensor_id}, twin/actuator/{id}, twin/unit/{id},
       brain/cmd/{target}, brain/alert, brain/error, guardian/control
  out: guardian/verdict/{sensor_id}, guardian/quarantine/{device_id}, guardian/status

Every topic name, key, threshold and id prefix comes from Settings (.env / config files).
"""
from __future__ import annotations

import logging
import math
import re
import time

from ..core.events import EVENT_SCHEMA_VERSION, parse_ts, sign_event, utcnow
from .actuators import AckTracker
from .envelope import (IdFactory, has_tag, iso_ms, open_envelope, seal, valid_id, valid_msg_id)
from .registry import CityRegistry, load_type_rules

log = logging.getLogger("guardian.brain")

READING_REQUIRED = ("id", "timestamp", "source", "sensor_id", "zone_id", "type", "value", "unit")


class TopicTemplate:
    """'city/{zone_id}/sensor/{type}/{sensor_id}' -> subscription 'city/+/sensor/+/+' + parser."""

    def __init__(self, template: str) -> None:
        self.template = template
        self.parts = template.split("/")
        self.subscription = "/".join("+" if re.fullmatch(r"\{\w+\}", p) else p for p in self.parts)

    def match(self, topic: str) -> dict | None:
        t = topic.split("/")
        if len(t) != len(self.parts):
            return None
        out = {}
        for p, v in zip(self.parts, t):
            m = re.fullmatch(r"\{(\w+)\}", p)
            if m:
                out[m.group(1)] = v
            elif p != v:
                return None
        return out

    def format(self, **kw) -> str:
        return self.template.format(**kw)


def brain_reason(native_reason: str, verdict: str, failed_step: int | None = None) -> str:
    """Native reason text -> the short reason words the Brain shows (free text is accepted)."""
    r = (native_reason or "").lower()
    if verdict == "trusted":
        return "consistent"
    if failed_step == 2 or r.startswith("replay"):
        return "replay"
    if failed_step == 5 or "device_quarantined" in r:
        return "quarantined"
    if "jumped" in r:
        return "implausible_jump"
    if "outside physical range" in r or "non-finite" in r:
        return "out_of_range"
    if "unit" in r and "invalid" in r:
        return "unit_mismatch"
    if "future" in r:
        return "stale"
    if "stuck_sensor" in r:
        return "frozen"
    if r.startswith("signature"):
        return "bad_hmac"
    if r.startswith("schema"):
        return "malformed"
    if r.startswith("anomaly"):
        return "anomaly"
    return "inconsistent"


class BrainAdapter:
    def __init__(self, settings, state, publisher, *, clock=None, mono=None) -> None:
        self.s, self.state, self.pub = settings, state, publisher
        self.clock = clock or utcnow                 # wall clock (freshness)
        self.mono = mono or time.monotonic           # monotonic clock (ack timeouts)
        self.ids = IdFactory(settings.run_id)
        self.registry = CityRegistry(type_rules=load_type_rules(settings.sensor_limits_file))
        self.t_reading = TopicTemplate(settings.topic_reading)
        self.t_actuator = TopicTemplate(settings.topic_actuator)
        self.t_unit = TopicTemplate(settings.topic_unit)
        self.t_cmd = TopicTemplate(settings.topic_brain_cmd)
        self.t_verdict = TopicTemplate(settings.topic_out_verdict)
        self.t_quarantine = TopicTemplate(settings.topic_out_quarantine)
        self.acks = AckTracker(settings.ack_timeout_s + settings.ack_grace_s,
                               settings.ack_startup_grace_s, self.mono())
        self.inconsistencies: dict[str, list[str]] = {}
        self._control_ids: dict[str, float] = {}
        self._pending: list[tuple[str, str, bool]] = []
        self._in_reading = False
        self.stats: dict[str, int] = {}
        # wire the native pipeline to the registry
        p = state.pipeline
        p.limits_provider = self.registry.limits_for
        p.anomaly.std_floor_provider = self.registry.noise_floor
        p.anomaly.z_ok, p.anomaly.span = settings.anomaly_z_ok, settings.anomaly_span
        p.anomaly_min_score = settings.anomaly_min_score
        p.plaus.step_window_s = settings.step_window_s
        p.anomaly.stuck_count = settings.frozen_count
        # liveness: last retained verdict body per sensor + time of the last verdict sent
        self._last_verdict: dict[str, dict] = {}
        self._last_verdict_t: float | None = None
        state.quarantine.listeners.append(self._on_quarantine_change)
        self.native_key = p.hmac_key  # internal: re-signs verified readings as native events

    # ------------------------------------------------------------------ subscriptions
    def subscriptions(self) -> list[str]:
        return [self.s.topic_twin_meta, self.t_reading.subscription, self.t_actuator.subscription,
                self.t_unit.subscription, self.t_cmd.subscription, self.s.topic_brain_alert,
                self.s.topic_brain_error, self.s.topic_guardian_control]

    # ------------------------------------------------------------------ helpers
    def _count(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    async def _audit(self, action: str, device_id: str | None, details: dict) -> None:
        self._count(action)
        await self.state.store.append_audit(action, device_id, details)

    def _body(self, **fields) -> dict:
        return {"id": self.ids.next(), "timestamp": iso_ms(self.clock()), "source": "guardian", **fields}

    def _send(self, topic: str, body: dict, retain: bool) -> None:
        self.pub.publish_raw(topic, seal(body, self.s.guardian_brain_key), retain=retain, qos=1)
        self._count("sent")

    def status_payload(self, status: str) -> str:
        return seal(self._body(status=status), self.s.guardian_brain_key)

    def announce(self, status: str = "online") -> None:
        """guardian/status (retained). Call right after the MQTT connection is up."""
        self.pub.publish_raw(self.s.topic_out_status, self.status_payload(status), retain=True, qos=1)

    def _fresh(self, ts_text) -> tuple[bool, str]:
        try:
            ts = parse_ts(ts_text)
        except Exception:  # noqa: BLE001
            return False, "bad timestamp"
        skew = abs((ts - self.clock()).total_seconds())
        if skew > self.s.clock_skew_s:
            return False, f"timestamp {skew:.1f}s from our clock (limit {self.s.clock_skew_s}s)"
        return True, "ok"

    # ------------------------------------------------------------------ router
    async def handle(self, topic: str, payload, retained: bool = False) -> str:
        """Route one MQTT message. Never raises: every failure ends in the audit log."""
        try:
            return await self._route(topic, payload, retained)
        except Exception as e:  # noqa: BLE001 - the Guardian must never crash on input
            log.exception("adapter error on %s", topic)
            await self._audit("adapter_error", None, {"topic": topic, "error": f"{type(e).__name__}: {e}"})
            return "error"

    async def _route(self, topic: str, payload, retained: bool) -> str:
        if topic == self.s.topic_twin_meta:
            return await self._on_meta(payload)
        f = self.t_reading.match(topic)
        if f is not None:
            return await self._on_reading(topic, f, payload, retained)
        f = self.t_actuator.match(topic)
        if f is not None:
            return await self._on_ack(topic, "actuator_id", f["actuator_id"], payload, retained)
        f = self.t_unit.match(topic)
        if f is not None:
            return await self._on_ack(topic, "unit_id", f["unit_id"], payload, retained)
        f = self.t_cmd.match(topic)
        if f is not None:
            return await self._on_command(topic, f["target_id"], payload, retained)
        if topic == self.s.topic_brain_alert:
            return await self._on_alert(payload, retained)
        if topic == self.s.topic_guardian_control:
            return await self._on_control(payload, retained)
        if topic == self.s.topic_brain_error:
            return await self._on_brain_error(payload)
        # anything else (city/{zone}/call included) never reaches the pipeline
        self._count("ignored_topic")
        return "ignored"

    # ------------------------------------------------------------------ twin/meta
    async def _on_meta(self, payload) -> str:
        tagged = has_tag(payload, self.s.hmac_tag)
        op = open_envelope(payload, self.s.twin_guardian_key, self.s.hmac_tag, accept_single_hmac=False)
        if op.body is None:
            await self._audit("meta_malformed", None, {"reason": op.reason})
            return "meta_malformed"
        if tagged and not op.ok:
            await self._audit("meta_rejected", None, {"reason": op.reason, "id": op.body.get("id")})
            return "meta_rejected"
        err = CityRegistry.validate_meta(op.body)
        if err:
            await self._audit("meta_invalid", None, {"error": err, "id": op.body.get("id")})
            return "meta_invalid"
        changed = self.registry.load_meta(op.body)
        if changed and self.s.seq_reset_on_new_meta:
            # a new city description = a Twin (re)start: its seq counters may restart at 1
            self.state.pipeline.replay._last_seq.clear()
        await self._audit("meta_loaded" if op.ok else "meta_loaded_unverified", None, {
            "id": op.body.get("id"), "verified": op.ok, "zones": len(self.registry.zones),
            "sensors": len(self.registry.sensors), "actuators": len(self.registry.actuators),
            "units": len(self.registry.units), "seq_reset": bool(changed and self.s.seq_reset_on_new_meta)})
        return "meta_loaded"

    # ------------------------------------------------------------------ readings
    def _native_unsigned(self, body: dict | None, topic_sensor: str) -> dict:
        b = body if isinstance(body, dict) else {}
        return {"schema_version": EVENT_SCHEMA_VERSION,
                "event_id": b.get("id") if valid_msg_id(b.get("id")) else "",
                "seq": 0, "device_id": topic_sensor, "sensor_type": str(b.get("type", "")),
                "value": 0.0, "unit": str(b.get("unit", "")), "ts": str(b.get("timestamp", "")),
                "signature": "bad"}

    async def _on_reading(self, topic: str, f: dict, payload, retained: bool) -> str:
        sid = f["sensor_id"]
        if retained:
            await self._audit("reading_retained_ignored", None, {"topic": topic})
            return "reading_retained"
        op = open_envelope(payload, self.s.twin_guardian_key, self.s.hmac_tag)
        if op.reason == "malformed":
            await self._audit("reading_malformed", None, {"topic": topic, "reason": "envelope/body not JSON"})
            return "malformed"
        if not op.ok:
            return await self._bad_hmac(topic, sid, op)
        b = op.body
        missing = [k for k in READING_REQUIRED if k not in b]
        val = b.get("value")
        if missing or isinstance(val, bool) or not isinstance(val, (int, float)) \
                or not math.isfinite(float(val)) or not valid_msg_id(b.get("id")):
            await self._audit("reading_malformed", None, {"topic": topic, "missing": missing,
                                                          "id": b.get("id")})
            return "malformed"
        if b["sensor_id"] != sid or b["zone_id"] != f.get("zone_id", b["zone_id"]) \
                or b["type"] != f.get("type", b["type"]):
            await self._audit("reading_topic_mismatch", None, {"topic": topic, "id": b["id"]})
            return "malformed"
        if self.registry.loaded:
            meta = self.registry.sensors.get(sid)
            if meta is None:
                await self._audit("reading_unknown_sensor", None, {"topic": topic, "id": b["id"]})
                return "unknown_id"
            if meta["zone_id"] != b["zone_id"] or meta["type"] != b["type"]:
                await self._audit("reading_meta_mismatch", sid, {"topic": topic, "id": b["id"]})
                return "malformed"
        ok, why = self._fresh(b["timestamp"])
        if not ok:
            await self._audit("reading_stale", sid, {"id": b["id"], "detail": why})
            return "stale"
        conv = self.registry.to_meta_unit(sid, b["type"], float(val), b["unit"])
        value, unit = conv if conv is not None else (float(val), str(b["unit"]))
        seq = b.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            # no Twin seq: our own strictly increasing counter (replay is then id-based only)
            seq = self.state.pipeline.replay._last_seq.get(sid, 0) + 1
        evt = sign_event({"schema_version": EVENT_SCHEMA_VERSION, "event_id": b["id"], "seq": seq,
                          "device_id": sid, "sensor_type": b["type"], "value": float(value),
                          "unit": unit, "ts": b["timestamp"], "signature": ""}, self.native_key)
        self._in_reading = True
        try:
            v = await self.state.pipeline.process(evt)
        finally:
            self._in_reading = False
        reason = brain_reason(v["reason"], v["verdict"], v.get("failed_step"))
        replay = reason == "replay"
        if not replay or self.s.brain_publish_replay_verdicts:
            body = self._body(correlation_id=b["id"], sensor_id=sid, verdict=v["verdict"], reason=reason,
                              trust_score=max(0.0, min(1.0, float(v["trust_score"]))),
                              details=str(v["reason"])[:280])
            # retained = the sensor's current state; a replay verdict must never overwrite it
            self._send(self.t_verdict.format(sensor_id=sid), body, retain=not replay)
            self._last_verdict_t = self.mono()
            if not replay:
                self._last_verdict[sid] = body
        self._count(f"verdict_{v['verdict']}")
        self._flush()
        return f"verdict_{v['verdict']}"

    async def _bad_hmac(self, topic: str, sid: str, op) -> str:
        """A bad or missing HMAC is evidence of an attack on the channel, not of a bad sensor:
        never counted toward quarantine, never retained."""
        rid = op.body.get("id") if isinstance(op.body, dict) and valid_msg_id(op.body.get("id")) else None
        await self._audit("reading_bad_hmac", None, {"topic": topic, "id": rid, "reason": op.reason})
        await self.state.pipeline.process(self._native_unsigned(op.body, sid))  # native verdict + audit
        if not valid_id(sid):
            return "bad_hmac_unattributable"
        body = self._body(sensor_id=sid, verdict="rejected", reason="bad_hmac", trust_score=0.0,
                          details=f"envelope {op.reason} on {topic}")
        if rid:
            body["correlation_id"] = rid
        self._send(self.t_verdict.format(sensor_id=sid), body, retain=False)
        return "bad_hmac"

    # ------------------------------------------------------------------ quarantine out
    async def _on_quarantine_change(self, device_id: str, state: str, reason: str, evidence: list) -> None:
        if state == "released":
            why = "reset" if "control:reset" in reason else "manual_release"
        elif reason == "inconsistent_ack" or not evidence:
            why = reason
        else:
            row = await self.state.store.get_verdict(evidence[-1])
            why = brain_reason(row.reason, row.verdict) if row else "rejected_readings"
        if not valid_id(device_id):
            return
        body = self._body(device_id=device_id, state=state, reason=why)
        item = (self.t_quarantine.format(device_id=device_id), body)
        if self._in_reading:
            self._pending.append(item)  # sent right after the verdict that caused it
        else:
            self._send(*item, retain=True)

    def _flush(self) -> None:
        while self._pending:
            topic, body = self._pending.pop(0)
            self._send(topic, body, retain=True)

    # ------------------------------------------------------------------ actuators / units
    async def _inconsistency(self, device_id: str, why: str) -> None:
        if not valid_id(device_id):
            return
        lst = self.inconsistencies.setdefault(device_id, [])
        lst.append(why)
        await self._audit("ack_inconsistency", device_id, {"detail": why, "count": len(lst)})
        if self.state.timeline is not None:
            self.state.timeline.add("ack_inconsistency", device_id, why, "warning")
        if len(lst) >= self.s.ack_quarantine_threshold and \
                not await self.state.store.is_quarantined(device_id):
            evidence = list(lst)
            self.inconsistencies[device_id] = []
            await self.state.quarantine.quarantine(device_id, "inconsistent_ack", evidence)

    async def _on_command(self, topic: str, target: str, payload, retained: bool) -> str:
        if retained:
            return "retained_ignored"
        op = open_envelope(payload, self.s.guardian_brain_key, self.s.hmac_tag, accept_single_hmac=False)
        if not op.ok:
            await self._audit("command_unverified", None, {"topic": topic, "reason": op.reason})
            return "command_unverified"
        b = op.body
        if b.get("target_id") != target or not valid_msg_id(b.get("id")) or not isinstance(b.get("action"), str):
            await self._audit("command_malformed", None, {"topic": topic, "id": b.get("id")})
            return "command_malformed"
        kind = self.acks.on_command(b["id"], target, b["action"], self.mono())
        if kind == "new":
            await self._audit("command_seen", target, {"id": b["id"], "action": b["action"]})
        return f"command_{kind}"

    async def _on_ack(self, topic: str, id_field: str, dev: str, payload, retained: bool) -> str:
        if retained:
            return "retained_ignored"  # state snapshot replayed by the broker, not a live ack
        op = open_envelope(payload, self.s.twin_guardian_key, self.s.hmac_tag)
        if not op.ok:
            await self._audit("ack_unverified", None, {"topic": topic, "reason": op.reason})
            return "ack_unverified"
        b = op.body
        if b.get(id_field) != dev:
            await self._audit("ack_malformed", None, {"topic": topic, "id": b.get("id")})
            return "ack_malformed"
        corr = b.get("correlation_id")
        if corr is None and b.get("result") is None:
            return "state_change"
        r = self.acks.on_ack(dev, corr or "", self.mono())
        if r is not None:
            await self._inconsistency(*r)
            return "ack_inconsistent"
        return "ack_ok"

    async def tick(self) -> None:
        """Call about once a second: commands never acknowledged, and the liveness refresh."""
        for dev, why in self.acks.expire(self.mono()):
            await self._inconsistency(dev, why)
        self.refresh_verdicts()

    def refresh_verdicts(self) -> int:
        """The Brain treats the Guardian as offline after 30 s without a verdict. If no verdict
        went out for VERDICT_REFRESH_S (Twin silent), re-send the current verdict of every known
        sensor: new id and timestamp, same verdict/reason/score/correlation_id, retained.
        Returns the number of verdicts re-sent."""
        every = self.s.verdict_refresh_s
        now = self.mono()
        if every <= 0 or not self._last_verdict or self._last_verdict_t is None \
                or now - self._last_verdict_t < every:
            return 0
        for sid, old in list(self._last_verdict.items()):
            body = self._body(**{k: v for k, v in old.items() if k not in ("id", "timestamp", "source")})
            body["details"] = ("refresh (no new reading): " + str(old.get("details", "")))[:280]
            self._send(self.t_verdict.format(sensor_id=sid), body, retain=True)
            self._last_verdict[sid] = body
        self._last_verdict_t = now
        self._count("verdict_refresh")
        return len(self._last_verdict)

    async def _on_alert(self, payload, retained: bool) -> str:
        if retained:
            return "retained_ignored"
        op = open_envelope(payload, self.s.guardian_brain_key, self.s.hmac_tag)
        if not op.ok:
            await self._audit("alert_unverified", None, {"reason": op.reason})
            return "alert_unverified"
        b = op.body
        target = b.get("target_id")
        if not self.registry.knows_device(target if isinstance(target, str) else ""):
            await self._audit("alert_unknown_target", None, {"target_id": target, "alert": b.get("alert")})
            return "alert_ignored"
        await self._audit("brain_alert", target, {"alert": b.get("alert"), "detail": b.get("detail"),
                                                  "id": b.get("id")})
        if self.s.alert_counts_as_inconsistency and b.get("alert") in (
                "actuator_no_effect", "actuator_not_responding"):
            await self._inconsistency(target, f"brain alert {b.get('alert')}: {b.get('detail')}")
        return "alert_ok"

    # ------------------------------------------------------------------ guardian/control
    async def _on_control(self, payload, retained: bool) -> str:
        if retained:
            await self._audit("control_retained_ignored", None, {})
            return "retained_ignored"
        op = open_envelope(payload, self.s.guardian_brain_key, self.s.hmac_tag)
        if not op.ok:
            await self._audit("control_rejected", None, {"reason": op.reason})
            return "control_rejected"
        b = op.body
        cid = b.get("id")
        if isinstance(cid, str):
            now = self.mono()
            self._control_ids = {k: t for k, t in self._control_ids.items() if now - t < 60}
            if cid in self._control_ids:
                await self._audit("control_replay", None, {"id": cid})
                return "control_replay"
            self._control_ids[cid] = now
        if "timestamp" in b:
            ok, why = self._fresh(b["timestamp"])
            if not ok:
                await self._audit("control_stale", None, {"id": cid, "detail": why})
                return "control_stale"
        if b.get("op") != "reset":
            await self._audit("control_unsupported", None, {"op": b.get("op"), "id": cid})
            return "control_unsupported"
        released = []
        for dev in await self.state.quarantine.quarantined_devices():
            await self.state.quarantine.release(dev, "control:reset")
            released.append(dev)
        self.state.pipeline.reset_all()
        self.acks.reset(self.mono())
        self.inconsistencies.clear()
        self._last_verdict.clear()
        await self._audit("control_reset", None, {"id": cid, "released": released})
        return "control_reset"

    async def _on_brain_error(self, payload) -> str:
        """brain/error: the Brain refused one of OUR messages -> audit it (not trusted input)."""
        op = open_envelope(payload, self.s.guardian_brain_key, self.s.hmac_tag)
        b = op.body or {}
        mid = b.get("message_id")
        if isinstance(mid, str) and mid.startswith(self.ids.prefix):
            await self._audit("brain_refused_our_message", None, {
                "code": b.get("code"), "message_id": mid, "detail": b.get("detail"),
                "verified": op.ok})
            return "brain_error_ours"
        return "brain_error_other"

