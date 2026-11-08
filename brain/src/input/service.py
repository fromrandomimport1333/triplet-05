"""Input service: checked external messages in, clean internal messages out.

Independent of MQTT: `handle()` takes (topic, payload, retained) and calls `publish`,
so the whole module is testable without a broker.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..common import config
from ..common.envelope import body_text, seal_multi
from ..common.ids import IdGenerator
from ..common.log import EventLogger, utc_now_iso
from ..common.models import (
    BrainError,
    BrainStatus,
    InternalAck,
    InternalCall,
    InternalLayerStatus,
    InternalMeta,
    InternalQuarantine,
    InternalReading,
    InternalTrust,
    TimelineEvent,
)
from .checks import Accepted, Checker, Rejected
from .registry import CityRegistry
from .topics import TopicRouter, fill

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain


class LayerTracker:
    """Online / offline of Twin and Guardian, from {layer}/status and Guardian's silence."""

    def __init__(self, guardian_silence_s: float, now: float):
        self.reported: dict[str, str | None] = {"twin": None, "guardian": None}
        self.effective: dict[str, str | None] = {"twin": None, "guardian": None}
        self.guardian_silence_s = guardian_silence_s
        self.started = now
        self.last_guardian_msg: float | None = None   # last live verdict / quarantine / status

    def compute(self, layer: str, now: float) -> tuple[str | None, str | None]:
        """Effective status and reason; (None, None) while nothing is known yet."""
        reported = self.reported[layer]
        if layer == "twin":
            return reported, None
        if reported == "offline":
            return "offline", "guardian/status offline"
        last = self.last_guardian_msg if self.last_guardian_msg is not None else self.started
        if self.guardian_silence_s and now - last > self.guardian_silence_s:
            return "offline", f"no verdict for {self.guardian_silence_s:g} s"
        if reported == "degraded":
            return "degraded", "guardian/status degraded"
        if reported == "online" or self.last_guardian_msg is not None:
            return "online", None
        return None, None


class InputService:
    def __init__(
        self,
        publish: Publisher,
        *,
        hmac_keys: dict[str, bytes],
        ed25519_key: Any = None,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        input_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        security_cfg: dict[str, Any] | None = None,
    ):
        self.cfg = input_cfg or config.load("input")
        self.topics = topics_cfg or config.load("topics")
        self.security = security_cfg or config.load("security")
        self._publish = publish
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("input", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.log_accepted = bool(log_cfg.get("log_accepted", False))

        self.router = TopicRouter(self.topics["external_in"])
        self.registry = CityRegistry()
        self.checker = Checker(
            router=self.router,
            registry=self.registry,
            hmac_keys=hmac_keys,
            hmacs_self=self.security.get("hmacs_self", "brain"),
            checks=self.cfg["checks"],
            field_map=self.cfg.get("field_map", {}),
            verdict_aliases=self.cfg.get("verdict_aliases", {}),
            guardian_signature=self.security.get("guardian_signature", {"mode": "hmac"}),
            ed25519_key=ed25519_key,
            clock=clock,
        )
        out = self.security.get("outgoing", {})
        self._seal_keys = (
            {layer: hmac_keys[layer] for layer in out.get("hmacs_for", []) if layer in hmac_keys}
            if out.get("seal", True)
            else None
        )
        self.ids = IdGenerator("in")
        self.err_ids = IdGenerator("in-err", width=5)
        self.layers = LayerTracker(self.cfg["layers"]["guardian_silence_s"], clock())
        self._quarantine: dict[str, str] = {}
        self._lock = threading.RLock()
        self._err_budget = (int(clock()), 0)
        self.stats = {"accepted": 0, "rejected": 0, "busy_s": 0.0, "since": clock()}

    # --- entry points -------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(r.subscription, r.qos) for r in self.router.routes]

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        """Process one external message. Never raises."""
        start = time.perf_counter()
        with self._lock:
            try:
                accepted = self.checker.run(topic, payload, retained)
            except Rejected as r:
                self._reject(topic, r)
            except Exception as e:  # a bug must not stop the loop
                self.log.error("input_internal_error", topic=topic, error=repr(e))
            else:
                try:
                    self._dispatch(accepted)
                    self.stats["accepted"] += 1
                except Exception as e:
                    self.log.error("input_dispatch_error", topic=topic, error=repr(e))
            self.stats["busy_s"] += time.perf_counter() - start

    def tick(self) -> None:
        """Called every second: Guardian silence, periodic stats."""
        with self._lock:
            self._refresh_layer("guardian")
            every = self.cfg["layers"].get("stats_every_s", 0)
            now = self.clock()
            elapsed = now - self.stats["since"]
            if every and elapsed >= every:
                n = self.stats["accepted"] + self.stats["rejected"]
                self.log.info(
                    "input_stats",
                    accepted=self.stats["accepted"],
                    rejected=self.stats["rejected"],
                    msgs_per_s=round(n / elapsed, 1),
                    avg_ms=round(1000 * self.stats["busy_s"] / n, 3) if n else None,
                    replay_cache=len(self.checker.replay),
                )
                self.stats = {"accepted": 0, "rejected": 0, "busy_s": 0.0, "since": now}

    def status_payload(self, status: str) -> str:
        """brain/status body, sealed (used for the Last Will and on every connect)."""
        body = BrainStatus(id=self.ids.next(), timestamp=utc_now_iso(), status=status)
        return self._external_payload(body.model_dump(exclude_none=True))

    # --- per kind ---------------------------------------------------------------------

    def _dispatch(self, a: Accepted) -> None:
        b = a.body
        if a.clock_offset_s is not None and abs(a.clock_offset_s) > self.cfg["checks"].get("drift_warn_s", 2):
            self.log.warning("clock_drift", source=a.source, offset_s=round(a.clock_offset_s, 3), correlation_id=b.id)
        if self.log_accepted:
            self.log.info("message_accepted", kind=a.kind, correlation_id=b.id, retained=a.retained)

        if a.kind == "meta":
            changed = self.registry.meta_id != b.id
            self.registry.load(b)
            self._internal("meta", InternalMeta(
                id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=b.id,
                zones=b.zones, sensors=b.sensors, actuators=b.actuators, units=b.units))
            if changed:
                summary = (f"city description: {len(b.zones)} zones, {len(b.sensors)} sensors, "
                           f"{len(b.actuators)} actuators, {len(b.units)} units")
                self.log.info("city_description_loaded", correlation_id=b.id, summary=summary)
                self._event("city_description_received", summary, b.id)

        elif a.kind == "reading":
            self._internal("reading", InternalReading(
                id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=b.id,
                sensor_id=b.sensor_id, zone_id=b.zone_id, type=b.type,
                value=b.value, unit=b.unit, origin=b.origin))

        elif a.kind == "call":
            self._internal("call", InternalCall(
                id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=b.id,
                call_id=b.call_id, zone_id=b.zone_id, category=b.category, text=b.text))

        elif a.kind in ("actuator", "unit"):
            self._internal("ack", InternalAck(
                id=self.ids.next(), timestamp=utc_now_iso(),
                correlation_id=b.correlation_id or b.id, message_id=b.id, kind=a.kind,
                target_id=b.actuator_id if a.kind == "actuator" else b.unit_id,
                result=b.result, state=b.state, reason=b.reason,
                status=getattr(b, "status", None), zone_id=getattr(b, "zone_id", None)))

        elif a.kind == "verdict":
            self._guardian_heard(a)
            self._internal("trust", InternalTrust(
                id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=b.correlation_id or b.id,
                sensor_id=b.sensor_id, verdict=b.verdict,
                trust_score=float(b.trust_score) if b.trust_score is not None else None,
                reason=b.reason), sensor_id=b.sensor_id)

        elif a.kind == "quarantine":
            self._guardian_heard(a)
            self._internal("quarantine", InternalQuarantine(
                id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=b.id,
                device_id=b.device_id, state=b.state, reason=b.reason), device_id=b.device_id)
            if self._quarantine.get(b.device_id) != b.state:
                self._quarantine[b.device_id] = b.state
                self.log.info("quarantine_changed", device_id=b.device_id, state=b.state,
                              reason=b.reason, correlation_id=b.id)
                verb = "quarantined" if b.state == "quarantined" else "released"
                self._event(f"device_{verb}", f"{b.device_id} {verb} by Guardian ({b.reason})", b.id)

        elif a.kind in ("twin_status", "guardian_status"):
            layer = a.source
            if b.status == "online" and self.layers.reported[layer] != "online":
                self.checker.seq.reset(layer)   # a restarted layer starts its seq over
            self.layers.reported[layer] = b.status
            if layer == "guardian" and b.status != "offline" and not a.retained:
                self.layers.last_guardian_msg = self.clock()
            self._refresh_layer(layer)

    def _guardian_heard(self, a: Accepted) -> None:
        if not a.retained:   # retained verdicts say nothing about Guardian being alive
            self.layers.last_guardian_msg = self.clock()
            self._refresh_layer("guardian")

    def _refresh_layer(self, layer: str) -> None:
        status, reason = self.layers.compute(layer, self.clock())
        if status is None or status == self.layers.effective[layer]:
            return
        self.layers.effective[layer] = status
        self._internal("layer_status", InternalLayerStatus(
            id=self.ids.next(), timestamp=utc_now_iso(), layer=layer, status=status, reason=reason),
            layer=layer)
        self.log.warning("layer_status", layer=layer, status=status, reason=reason)
        self._event(f"layer_{status}", f"{layer} is {status}" + (f" ({reason})" if reason else ""))

    # --- outgoing ---------------------------------------------------------------------

    def _internal(self, name: str, msg, **topic_vars: str) -> None:
        spec = self.topics["internal"][name]
        self._publish(fill(spec["topic"], **topic_vars), msg.to_json(), int(spec["qos"]), bool(spec["retain"]))

    def _external(self, name: str, body: dict[str, Any]) -> None:
        spec = self.topics["external_out"][name]
        self._publish(spec["topic"], self._external_payload(body), int(spec["qos"]), bool(spec["retain"]))

    def _external_payload(self, body: dict[str, Any]) -> str:
        return seal_multi(body, self._seal_keys) if self._seal_keys else body_text(body)

    def _event(self, event: str, summary: str, correlation_id: str | None = None) -> None:
        body = TimelineEvent(id=self.ids.next(), timestamp=utc_now_iso(),
                             correlation_id=correlation_id, event=event, summary=summary)
        self._external("event", body.model_dump(exclude_none=True))

    def _reject(self, topic: str, r: Rejected) -> None:
        self.stats["rejected"] += 1
        self.log.warning("message_rejected", code=r.code, topic=topic, message_id=r.message_id,
                         correlation_id=r.message_id, detail=r.detail)
        second = int(self.clock())
        start, count = self._err_budget
        if second != start:
            start, count = second, 0
        limit = self.cfg.get("errors", {}).get("max_published_per_s", 0)
        self._err_budget = (start, count + 1)
        if limit and count >= limit:
            return
        body = BrainError(id=self.err_ids.next(), timestamp=utc_now_iso(), code=r.code,
                          topic=topic, message_id=r.message_id, detail=r.detail)
        self._external("error", body.model_dump(exclude_none=True))
