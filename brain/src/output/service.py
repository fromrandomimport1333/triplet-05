"""Output service: decisions in, safe and signed commands out (team plan 05, sections 4 and 5).

Independent of MQTT, like the other services: `handle()` takes (topic, payload, retained),
`tick()` runs the timeouts, and the service calls `publish`. Nothing here ever blocks.

For every action of a decision:
  safety rules S1..S6 (+ S7: Twin offline) -> refused (outcome + event action_refused)
                      -> BrainCommand sealed for Twin and Guardian on brain/cmd/{target} (QoS 1),
                         outcome sent + event command_sent
  ack `done`          -> outcome acknowledged + event command_acknowledged, effect check starts
  ack rejected/failed -> outcome failed (detail = reason) + event command_failed (+ alert command_rejected)
  no ack in timeout_s -> resent once with the SAME id (fresh timestamp), then outcome failed (detail no_ack)
                         + event command_failed + alert actuator_not_responding
  no effect in window -> outcome failed (detail no_effect) + event command_failed + alert actuator_no_effect

A decision id is executed once; a duplicate or late acknowledgement is ignored.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from ..common import config
from ..common.envelope import seal_for
from ..common.events import publish_event
from ..common.ids import IdGenerator
from ..common.log import EventLogger, iso_utc
from ..common.models import (
    BrainAlert,
    BrainCommand,
    Decision,
    Incident,
    InternalAck,
    Outcome,
    ProposalAction,
)
from .effects import EffectCheck, EffectTracker
from .safety import CheckInput, Device, Refusal, SafetyRules, Source

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain


def tolerant(model: type[BaseModel], msg: dict[str, Any]) -> tuple[BaseModel, list[str]]:
    """Validate `msg`, dropping fields the model does not know (returned for the log), so an
    additive change on the publisher's side never stops a command."""
    extra = [k for k in msg if k not in model.model_fields]
    return model.model_validate({k: v for k, v in msg.items() if k in model.model_fields}), extra


class CommandIds:
    """Command ids the Twin never sees twice (it answers a reused id with the old acknowledgement).

    id_start auto: brain-<session>-000001, session = start time in seconds, base 36. A restart
    takes more than a second, so it always gets a new session: ids are unique across restarts,
    days and midnight. An integer id_start gives brain-000042 style ids (tests, fixed numbering).
    """

    def __init__(self, prefix: str, start: Any, now: float):
        if start == "auto":
            self._prefix, self._n = f"{prefix}-{_base36(int(now))}", 0
        else:
            self._prefix, self._n = prefix, max(1, int(start)) - 1
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            self._n += 1
            return f"{self._prefix}-{self._n:06d}"


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


@dataclass(slots=True)
class Pending:
    command: BrainCommand
    topic: str
    payload: str
    qos: int
    decision: Decision
    device: Device
    label: str                      # "open valve-01 (flood at zone-01)" for the timeline
    sent_at: float
    attempts: int = 1
    effect: EffectCheck | None = None


class OutputService:
    def __init__(
        self,
        publish: Publisher,
        *,
        seal_keys: dict[str, bytes] | None = None,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        output_cfg: dict[str, Any] | None = None,
        safety_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
    ):
        self.cfg = output_cfg or config.load("output")
        self.safety_cfg = safety_cfg or config.load("safety")
        self.topics = topics_cfg or config.load("topics")
        self._publish = publish
        self.clock = clock
        self.seal_keys = seal_keys
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-output", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.rules = SafetyRules(self.safety_cfg)
        self.effects = EffectTracker(self.cfg.get("effects", {}))
        cmd_cfg = self.cfg.get("commands", {})
        self.cmd_ids = CommandIds(cmd_cfg.get("id_prefix", "brain"), cmd_cfg.get("id_start", "auto"), clock())
        self.ids = IdGenerator("out")
        acks = self.cfg.get("acks", {})
        self.ack_timeout_s = float(acks.get("timeout_s", 4))
        self.retries = int(acks.get("retries", 1))
        self._offline_states = set(self.safety_cfg.get("guardian_offline_states", ["offline"]))
        self._twin_offline_states = set(self.safety_cfg.get("twin_offline_states", ["offline"]))
        self._unverified_trust = float(self.safety_cfg.get("evidence", {}).get("unverified_trust", 1.0))
        self._s4_any = self.safety_cfg.get("s4_scope", "same_action") == "any_action"

        self._lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.zone_names: dict[str, str] = {}
        self.sensors: dict[str, tuple[str, str]] = {}            # sensor_id -> (zone_id, type)
        self.has_meta = False
        self.quarantined: set[str] = set()
        self.trust: dict[str, tuple[str, float | None]] = {}     # sensor_id -> (verdict, score)
        self.layers: dict[str, str] = {}
        self.safe_mode: bool | None = None
        self.incidents: dict[str, Incident] = {}
        self._decisions: dict[str, float] = {}                   # decision id -> time executed
        self._last_sent: dict[tuple[str, str], float] = {}       # (target, action or "*") -> time
        self.pending: dict[str, Pending] = {}                    # command id -> waiting for an ack
        self._finished: dict[str, float] = {}                    # command id -> time finished
        self._watch: dict[str, Pending] = {}                     # command id -> effect being checked
        self.stats = {"sent": 0, "refused": 0, "acknowledged": 0, "failed": 0, "retried": 0}

    # --- inputs -------------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        wild = lambda spec: spec["topic"].split("{")[0] + "+"   # noqa: E731
        return [
            (t["meta"]["topic"], 1),
            (t["decision"]["topic"], 1),
            (t["incident"]["topic"], 1),
            (t["ack"]["topic"], 1),
            (t["reading"]["topic"], 0),
            (wild(t["quarantine"]), 1),
            (wild(t["trust"]), 1),
            (wild(t["layer_status"]), 1),
        ]

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        """Process one internal message. Never raises."""
        try:
            msg = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            self.log.warning("internal_message_not_json", topic=topic)
            return
        if not isinstance(msg, dict):
            return
        t = self.topics["internal"]
        prefix = lambda name: t[name]["topic"].split("{")[0]   # noqa: E731
        try:
            with self._lock:
                if topic == t["reading"]["topic"]:
                    self._on_reading(msg)
                elif topic == t["decision"]["topic"]:
                    self._on_decision(msg)
                elif topic == t["ack"]["topic"]:
                    self._on_ack(msg)
                elif topic == t["incident"]["topic"]:
                    self._on_incident(msg)
                elif topic == t["meta"]["topic"]:
                    self._on_meta(msg)
                elif topic.startswith(prefix("quarantine")):
                    self._on_quarantine(msg)
                elif topic.startswith(prefix("trust")):
                    self._on_trust(msg)
                elif topic.startswith(prefix("layer_status")):
                    self._on_layer(msg)
        except (KeyError, TypeError, ValueError) as e:   # pydantic ValidationError is a ValueError
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])
        except Exception as e:  # a bug must never stop the loop
            self.log.error("output_internal_error", topic=topic, error=repr(e))

    def tick(self) -> None:
        """Ack timeouts, retries, effect checks, memory clean-up. Call every tick_s."""
        try:
            with self._lock:
                now = self.clock()
                for p in list(self.pending.values()):
                    if now - p.sent_at < self.ack_timeout_s:
                        continue
                    if p.attempts <= self.retries:
                        self._retry(p, now)
                    else:
                        self._no_ack(p, now)
                ok, failed = self.effects.evaluate(now)
                for c in ok:
                    self._effect_seen(c)
                for c in failed:
                    self._no_effect(c)
                self._purge(now)
        except Exception as e:
            self.log.error("output_tick_error", error=repr(e))

    # --- state --------------------------------------------------------------------------

    def _on_meta(self, msg: dict[str, Any]) -> None:
        devices: dict[str, Device] = {}
        for a in msg.get("actuators", []):
            devices[a["actuator_id"]] = Device(a["actuator_id"], "actuator", a["type"], a.get("zone_id"),
                                               list(a.get("controls") or []), list(a.get("actions") or []))
        for u in msg.get("units", []):
            devices[u["unit_id"]] = Device(u["unit_id"], "unit", u["type"], u.get("home_zone"))
        self.devices = devices
        self.sensors = {s["sensor_id"]: (s["zone_id"], s["type"]) for s in msg.get("sensors", [])}
        self.zone_names = {z["zone_id"]: z.get("name") or z["zone_id"] for z in msg.get("zones", [])}
        self.has_meta = True

    def _on_reading(self, msg: dict[str, Any]) -> None:
        self.effects.add_reading(msg["sensor_id"], msg["zone_id"], msg["type"], float(msg["value"]), self.clock())

    def _on_quarantine(self, msg: dict[str, Any]) -> None:
        if msg["state"] == "quarantined":
            self.quarantined.add(msg["device_id"])
        else:
            self.quarantined.discard(msg["device_id"])

    def _on_trust(self, msg: dict[str, Any]) -> None:
        score = msg.get("trust_score")
        self.trust[msg["sensor_id"]] = (msg["verdict"], float(score) if score is not None else None)

    def _on_incident(self, msg: dict[str, Any]) -> None:
        inc, _ = tolerant(Incident, msg)
        if inc.status == "closed":
            self.incidents.pop(inc.incident_id, None)
        else:
            self.incidents[inc.incident_id] = inc

    def _on_layer(self, msg: dict[str, Any]) -> None:
        layer, status = msg["layer"], msg["status"]
        was_status = self.layers.get(layer)
        self.layers[layer] = status
        if layer == "twin":
            if was_status in self._twin_offline_states and not self.twin_offline() and self._last_sent:
                # the Twin restarted: its actuators are back to their defaults, the cooldowns are moot
                self._last_sent.clear()
                self.log.info("cooldowns_cleared", reason="twin back online")
            return
        if layer != "guardian":
            return
        safe = status in self._offline_states
        if safe == bool(self.safe_mode) and self.safe_mode is not None:
            return
        was = self.safe_mode
        self.safe_mode = safe
        if safe:
            self.log.warning("safe_mode_on", reason=msg.get("reason"))
            self._event("safe_mode_on", "SAFE MODE ON: Guardian offline, risky actions need 2 sensor types"
                        + (f" ({msg['reason']})" if msg.get("reason") else ""))
        elif was:
            self.log.info("safe_mode_off")
            self._event("safe_mode_off", f"safe mode off: Guardian {status}")

    def guardian_offline(self) -> bool:
        return self.layers.get("guardian") in self._offline_states

    def twin_offline(self) -> bool:
        return self.layers.get("twin") in self._twin_offline_states

    # --- decisions ----------------------------------------------------------------------

    def _on_decision(self, msg: dict[str, Any]) -> None:
        d, extra = tolerant(Decision, msg)
        if extra:
            self.log.warning("decision_extra_fields_ignored", decision_id=d.id, fields=extra)
        if d.id in self._decisions:
            self.log.info("decision_duplicate_ignored", decision_id=d.id, correlation_id=d.correlation_id)
            return
        self._decisions[d.id] = self.clock()
        inc = self.incidents.get(d.incident_id)
        self.log.info("decision_received", decision_id=d.id, correlation_id=d.correlation_id,
                      incident_id=d.incident_id, actions=len(d.actions), mode=d.mode)
        for action in d.actions:
            try:
                self._execute(d, action, inc)
            except Exception as e:  # one action never stops the others
                self.log.error("action_failed_internally", decision_id=d.id, target_id=action.target_id, error=repr(e))

    def sources(self, d: Decision, inc: Incident | None) -> list[Source]:
        """Evidence of the decision's incident, with Guardian's current verdict applied."""
        out: list[Source] = []
        seen: set[str] = set()
        for ev in (inc.evidence if inc else []):
            trust = float(ev.trust)
            stype = None
            if ev.kind == "reading":
                stype = self.sensors.get(ev.source_id, (None, None))[1]
                verdict = self.trust.get(ev.source_id)
                if verdict and verdict[0] == "rejected":
                    trust = 0.0
            out.append(Source(ev.kind, ev.source_id, trust, stype))
            seen.add(ev.source_id)
        for sid in d.evidence_sensors:
            if sid in seen:
                continue
            verdict = self.trust.get(sid)
            if verdict is None:
                trust = self._unverified_trust
            elif verdict[0] == "rejected":
                trust = 0.0
            else:
                trust = verdict[1] if verdict[1] is not None else 1.0
            out.append(Source("reading", sid, trust, self.sensors.get(sid, (None, None))[1]))
            seen.add(sid)
        return out

    def _execute(self, d: Decision, a: ProposalAction, inc: Incident | None) -> None:
        now = self.clock()
        device = self.devices.get(a.target_id)
        key = (a.target_id, "*" if self._s4_any else a.action)
        where = f"{inc.type} at {self._zone(inc.zone_id)}" if inc else f"incident {d.incident_id}"
        label = f"{a.action} {a.target_id} ({where})"
        refusal = self.rules.check(CheckInput(
            target_id=a.target_id, action=a.action, device=device, has_meta=self.has_meta,
            quarantined=a.target_id in self.quarantined, sources=self.sources(d, inc),
            guardian_offline=self.guardian_offline() or d.mode == "safe",
            last_sent=self._last_sent.get(key), now=now,
        ))
        if refusal is None and device is None:     # S5 turned off: still never command an unknown id
            refusal = Refusal("S5", f"unknown target {a.target_id}")
        if refusal is None and self.rules.on("S7") and self.twin_offline():
            refusal = Refusal("S7", "Twin offline: the command would not be executed")
        if refusal is not None:
            self._refuse(d, a, refusal, label)
            return

        params: dict[str, Any] = dict(a.params)
        if (device.kind == "unit" and a.action == "dispatch" and inc is not None
                and self.cfg.get("commands", {}).get("fill_dispatch_zone", True)):
            if params.get("zone_id") not in (None, inc.zone_id):
                self.log.warning("dispatch_zone_replaced", target_id=a.target_id, asked=params.get("zone_id"),
                                 incident_zone=inc.zone_id, decision_id=d.id)
            params["zone_id"] = inc.zone_id
        cmd = BrainCommand(id=self.cmd_ids.next(), timestamp=self._iso(), correlation_id=d.correlation_id,
                           target_id=a.target_id, action=a.action, parameters=params)
        spec = self.topics["external_out"]["cmd"]
        topic = spec["topic"].replace("{target_id}", a.target_id)
        payload = self._seal(cmd.model_dump(exclude_none=True))
        qos = int(spec["qos"])
        self._publish(topic, payload, qos, bool(spec.get("retain", False)))
        self._last_sent[key] = now
        p = Pending(command=cmd, topic=topic, payload=payload, qos=qos, decision=d, device=device,
                    label=label, sent_at=now)
        espec = self.effects.spec_for(device.kind, device.type, a.action)
        if espec is not None:
            zones = (device.controls or [device.zone_id]) if device.kind == "actuator" \
                else [params.get("zone_id") or device.zone_id]
            p.effect = self.effects.start(cmd.id, espec, [z for z in zones if z], now)
        self.pending[cmd.id] = p
        self.stats["sent"] += 1
        self.log.info("command_sent", command_id=cmd.id, correlation_id=d.correlation_id, decision_id=d.id,
                      target_id=a.target_id, action=a.action, parameters=params)
        self._outcome(d, a.target_id, a.action, "sent", command_id=cmd.id)
        self._event("command_sent", f"command {cmd.id} sent: {label}" + self._params_text(params), d)

    def _refuse(self, d: Decision, a: ProposalAction, r: Refusal, label: str) -> None:
        self.stats["refused"] += 1
        self.log.warning("action_refused", decision_id=d.id, correlation_id=d.correlation_id,
                         target_id=a.target_id, action=a.action, rule=r.rule, detail=r.detail)
        self._outcome(d, a.target_id, a.action, "refused", rule=r.rule, detail=r.detail)
        self._event("action_refused", f"REFUSED {label}: {r.rule} {r.detail}", d)

    # --- acknowledgements ---------------------------------------------------------------

    def _on_ack(self, msg: dict[str, Any]) -> None:
        ack, _ = tolerant(InternalAck, msg)
        if ack.result is None:            # a plain state update, not an acknowledgement
            return
        p = self.pending.get(ack.correlation_id)
        if p is None:
            if ack.correlation_id in self._finished or ack.correlation_id in self._watch:
                self.log.info("ack_duplicate_ignored", command_id=ack.correlation_id, result=ack.result)
            return
        if ack.target_id != p.command.target_id:
            self.log.warning("ack_target_mismatch", command_id=p.command.id, expected=p.command.target_id,
                             got=ack.target_id)
            return
        del self.pending[p.command.id]
        d, cmd = p.decision, p.command
        if ack.result == "done":
            self.stats["acknowledged"] += 1
            detail = f"state {ack.state}" if ack.state else (f"status {ack.status}" if ack.status else "done")
            self.log.info("command_acknowledged", command_id=cmd.id, correlation_id=d.correlation_id,
                          target_id=cmd.target_id, state=ack.state, attempts=p.attempts)
            self._outcome(d, cmd.target_id, cmd.action, "acknowledged", command_id=cmd.id, detail=detail)
            self._event("command_acknowledged", f"{cmd.target_id} acknowledged {cmd.id}: {p.label}, {detail}", d)
            if p.effect is not None and self.effects.arm(cmd.id, self.clock()) is not None:
                self._watch[cmd.id] = p
            else:
                self._finished[cmd.id] = self.clock()
            return
        self.effects.cancel(cmd.id)
        self._finished[cmd.id] = self.clock()
        self.stats["failed"] += 1
        reason = ack.reason or ack.result
        self.log.warning("command_failed", command_id=cmd.id, correlation_id=d.correlation_id,
                         target_id=cmd.target_id, result=ack.result, reason=reason)
        self._outcome(d, cmd.target_id, cmd.action, "failed", command_id=cmd.id, detail=reason)
        self._event("command_failed", f"FAILED {p.label}: Twin answered {ack.result} ({reason})", d)
        if self.cfg.get("alerts", {}).get("on_rejected", True):
            self._alert("command_rejected", cmd, f"{cmd.action} {ack.result}: {reason}")

    def _retry(self, p: Pending, now: float) -> None:
        p.attempts += 1
        p.sent_at = now
        self.stats["retried"] += 1
        # same id (the Twin executes an id at most once) but a fresh timestamp and seal: the original
        # bytes may already be older than the Twin's command age limit, and it would refuse them
        p.command = p.command.model_copy(update={"timestamp": self._iso()})
        p.payload = self._seal(p.command.model_dump(exclude_none=True))
        self._publish(p.topic, p.payload, p.qos, False)
        self.log.warning("command_retried", command_id=p.command.id, correlation_id=p.decision.correlation_id,
                         target_id=p.command.target_id, attempt=p.attempts)
        self._event("command_retried", f"no ack from {p.command.target_id} after {self.ack_timeout_s:g} s: "
                    f"{p.command.id} resent (same id)", p.decision)

    def _no_ack(self, p: Pending, now: float) -> None:
        cmd, d = p.command, p.decision
        del self.pending[cmd.id]
        self.effects.cancel(cmd.id)
        self._finished[cmd.id] = now
        self.stats["failed"] += 1
        why = f"no acknowledgement after {p.attempts} attempts ({p.attempts * self.ack_timeout_s:g} s)"
        self.log.error("command_failed", command_id=cmd.id, correlation_id=d.correlation_id,
                       target_id=cmd.target_id, reason="no_ack", attempts=p.attempts)
        self._outcome(d, cmd.target_id, cmd.action, "failed", command_id=cmd.id, detail="no_ack")
        self._event("command_failed", f"FAILED {p.label}: {why}", d)
        self._alert("actuator_not_responding", cmd, why)

    # --- effect check -------------------------------------------------------------------

    def _effect_seen(self, c: EffectCheck) -> None:
        p = self._watch.pop(c.command_id, None)
        self._finished[c.command_id] = self.clock()
        if p is None:
            return
        s = c.seen or {}
        word = "fell" if c.expect == "falls" else "rose"
        self.log.info("effect_confirmed", command_id=c.command_id, correlation_id=p.decision.correlation_id, **s)
        self._event("effect_confirmed", f"effect confirmed for {p.label}: {s.get('sensor_id')} {word} "
                    f"from {s.get('from')} to {s.get('value')}", p.decision)

    def _no_effect(self, c: EffectCheck) -> None:
        p = self._watch.pop(c.command_id, None)
        self._finished[c.command_id] = self.clock()
        if p is None:
            return
        cmd, d = p.command, p.decision
        self.stats["failed"] += 1
        verb = "fall" if c.expect == "falls" else "rise"
        why = f"{c.metric} in {', '.join(c.zones)} did not {verb} within {self.effects.window_s:g} s"
        self.log.warning("command_no_effect", command_id=cmd.id, correlation_id=d.correlation_id,
                         target_id=cmd.target_id, detail=why)
        self._outcome(d, cmd.target_id, cmd.action, "failed", command_id=cmd.id, detail="no_effect")
        self._event("command_failed", f"NO EFFECT {p.label}: {why}", d)
        self._alert("actuator_no_effect", cmd, why)

    # --- helpers ------------------------------------------------------------------------

    def _purge(self, now: float) -> None:
        mem = self.cfg.get("memory", {})
        keep_d = float(mem.get("decision_ids_s", 3600))
        keep_c = float(mem.get("finished_commands_s", 600))
        for k, t in list(self._decisions.items()):
            if now - t > keep_d:
                del self._decisions[k]
        for k, t in list(self._finished.items()):
            if now - t > keep_c:
                del self._finished[k]
        cooldown = float(self.safety_cfg.get("s4_cooldown_s", 60))
        for k, t in list(self._last_sent.items()):
            if now - t > cooldown:
                del self._last_sent[k]

    def _iso(self) -> str:
        """From the service clock (the Twin checks command age)."""
        return iso_utc(self.clock())

    def _zone(self, zone_id: str) -> str:
        name = self.zone_names.get(zone_id)
        return f"{zone_id} ({name})" if name and name != zone_id else zone_id

    @staticmethod
    def _params_text(params: dict[str, Any]) -> str:
        return (" [" + ", ".join(f"{k}={v}" for k, v in params.items()) + "]") if params else ""

    def _seal(self, body: dict[str, Any]) -> str:
        return seal_for(body, self.seal_keys)

    def _outcome(self, d: Decision, target_id: str, action: str, status: str, **fields: Any) -> None:
        o = Outcome(id=self.ids.next(), timestamp=self._iso(), correlation_id=d.correlation_id,
                    incident_id=d.incident_id, decision_id=d.id, target_id=target_id, action=action,
                    status=status, **fields)
        spec = self.topics["internal"]["outcome"]
        self._publish(spec["topic"], o.to_json(), int(spec["qos"]), bool(spec["retain"]))

    def _event(self, event: str, summary: str, d: Decision | None = None) -> None:
        publish_event(self._publish, self.topics, self.ids, self.seal_keys, event, summary,
                      correlation_id=d.correlation_id if d else None,
                      incident_id=d.incident_id if d else None, timestamp=self._iso())

    def _alert(self, alert: str, cmd: BrainCommand, detail: str) -> None:
        body = BrainAlert(id=self.ids.next(), timestamp=self._iso(), correlation_id=cmd.correlation_id,
                          alert=alert, target_id=cmd.target_id, detail=f"{cmd.id}: {detail}")
        spec = self.topics["external_out"]["alert"]
        self._publish(spec["topic"], self._seal(body.model_dump(exclude_none=True)), int(spec["qos"]),
                      bool(spec["retain"]))
        self.log.warning("alert_sent", alert=alert, target_id=cmd.target_id, command_id=cmd.id)
