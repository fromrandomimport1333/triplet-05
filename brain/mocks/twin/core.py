"""Deterministic city simulator. No MQTT, no clock, no signing: it only produces messages.

Time advances only when `step()` is called. Every call that changes something returns
the list of *outputs* the transport must publish (readings, calls, acks, unit states).
Each output is a plain dict with a `kind`; `messages.py` turns them into wire bodies.

Scenario effects are small scripted perturbations, not city physics.
"""

from __future__ import annotations

import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from random import Random
from threading import RLock
from functools import cache
from pathlib import Path
from typing import Any

from .city import UNIT_ACTIONS, City, load_city

SCENARIOS = ("calm", "flood", "fire", "accident", "traffic-jam", "cooking")
TRACES_DIR = Path(__file__).with_name("traces")
# trace column -> sensor type it is added to (rises above the calm value, one row per tick)
TRACE_SIGNALS = {"smoke": "smoke", "co": "co", "co2_ppm": "co2", "temperature": "temperature"}


@cache
def trace(name: str) -> tuple[dict[str, float], ...]:
    """A replayed NIST smoke alarm test (tools/build_cases.py): rises per second; empty if absent."""
    path = TRACES_DIR / f"{name}.csv"
    if not path.is_file():
        return ()
    with open(path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].startswith("#")]
    head = rows[0]
    return tuple({k: float(v) for k, v in zip(head, r, strict=True) if k in TRACE_SIGNALS} for r in rows[1:])


def _add_trace(values: dict[str, float], name: str, age: int) -> None:
    rows = trace(name)
    if not rows:
        return
    row = rows[min(max(age, 0), len(rows) - 1)]          # the last second holds after the end
    for col, stype in TRACE_SIGNALS.items():
        if stype in values:
            values[stype] += row[col]

FAULT_MODES = (
    # reading faults
    "malformed", "impossible", "wrong_type", "wrong_unit", "missing", "freeze", "duplicate",
    "old", "replay", "delay", "late", "falsify", "unknown_id", "bad_hmac",
    # actuator / unit faults
    "reject", "failure", "lost_ack", "no_effect",
    # actuator modes (team plan 06 section 5); `ok` is the absence of a fault, `stuck` is `no_effect`
    "slow", "no_ack", "stuck", "reject_quarantined",
)
_NOT_READING_FAULTS = {"reject", "failure", "lost_ack", "no_effect", "slow", "no_ack", "reject_quarantined"}
CALL_CATEGORIES = ("fire", "accident", "flood", "medical", "other")   # as in the Brain's call model
ACTUATOR_MODES = ("ok", "slow", "no_ack", "stuck", "reject_quarantined")
# Actions that put an actuator back to its default: they end the effect of the previous command.
_RESTING_ACTIONS = {"close", "normal"}


@cache
def _default_city() -> City:
    return load_city()


class Fault:
    def __init__(self, mode: str, tick: int, end_tick: int | None = None, device_id: str | None = None,
                 delay_ticks: int = 2, value: float = 1.8):
        if mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {mode!r}")
        mode = "no_effect" if mode == "stuck" else mode
        if end_tick is not None and end_tick < tick:
            raise ValueError("end_tick must not precede tick")
        self.mode, self.tick, self.end_tick, self.device_id = mode, tick, end_tick, device_id
        self.delay_ticks, self.value = delay_ticks, value

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fault":
        return cls(**d)

    def matches(self, tick: int, device_id: str | None = None) -> bool:
        last = self.end_tick if self.end_tick is not None else self.tick
        return self.tick <= tick <= last and (self.device_id is None or self.device_id == device_id)


class SimConfig:
    def __init__(self, *, seed: int = 7, run_id: str = "twin", epoch: datetime | None = None, tick_s: float = 1.0,
                 command_delay_ticks: int = 2, noise: float = 0.02, travel_ticks: int = 2, scene_ticks: int = 3,
                 scenario: str = "calm", scenario_after_ticks: int = 5, clock: str = "wall",
                 actuator_modes: dict[str, str] | None = None, slow_ack_s: float = 5.0,
                 rush_hour: dict[str, Any] | None = None, call_texts: dict[str, list[str]] | None = None,
                 calls: list[dict[str, Any]] | None = None, city: City | None = None, scenario_zone: str | None = None, faults: list[Fault] | None = None):
        if clock not in ("wall", "simulated"):
            raise ValueError(f"unknown clock {clock!r}")
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.seed, self.run_id, self.tick_s = seed, run_id, tick_s
        self.epoch = epoch or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.command_delay_ticks, self.noise = command_delay_ticks, noise
        self.travel_ticks, self.scene_ticks = travel_ticks, scene_ticks
        self.scenario, self.scenario_after_ticks, self.clock = scenario, scenario_after_ticks, clock
        for mode in (actuator_modes or {}).values():
            if mode not in ACTUATOR_MODES:
                raise ValueError(f"unknown actuator mode {mode!r}")
        self.actuator_modes, self.slow_ack_s = dict(actuator_modes or {}), slow_ack_s
        self.city, self.scenario_zone = city or _default_city(), scenario_zone
        if scenario_zone is not None and scenario_zone not in self.city.zone_ids:
            raise ValueError(f"unknown scenario zone {scenario_zone!r}")
        self.faults = faults or []
        self.rush_hour = _rush_hour(rush_hour)
        self.call_texts = {c: list(t) for c, t in (call_texts or {}).items()}
        for category, texts in self.call_texts.items():
            if category not in CALL_CATEGORIES or not texts or not all(isinstance(x, str) for x in texts):
                raise ValueError(f"call_texts: {category!r} needs a known category and a list of texts")
        self.calls = [_scripted_call(c, self.city.zone_ids) for c in calls or []]


def _rush_hour(raw: dict[str, Any] | None) -> dict[str, float] | None:
    """Daily traffic pattern: a smooth bump between start_tick and end_tick (speed down, density and flow up)."""
    if not raw:
        return None
    out = {"speed_drop": 0.4, "density_rise": 2.0, "flow_rise": 0.8, **raw}
    if "start_tick" not in out or "end_tick" not in out or not out["start_tick"] < out["end_tick"]:
        raise ValueError("rush_hour needs start_tick < end_tick")
    if not 0 <= out["speed_drop"] < 1 or out["density_rise"] < 0 or out["flow_rise"] < 0:
        raise ValueError("rush_hour: speed_drop must be in [0, 1), the rises must not be negative")
    return out


def _scripted_call(raw: dict[str, Any], zones: list[str]) -> dict[str, Any]:
    call = {"end_tick": raw.get("tick"), "count": 1, "text": None, **raw}
    if not isinstance(call.get("tick"), int) or call["end_tick"] < call["tick"] or call["count"] < 1:
        raise ValueError(f"calls: bad tick / end_tick / count in {raw!r}")
    if call.get("zone_id") not in zones or call.get("category") not in CALL_CATEGORIES:
        raise ValueError(f"calls: unknown zone or category in {raw!r}")
    return call


class ConflictError(ValueError):
    pass


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Twin:
    def __init__(self, config: SimConfig | None = None):
        self._lock = RLock()
        self.reset(config or SimConfig())

    # --- time and logging -----------------------------------------------------------

    def timestamp(self, tick: int) -> str:
        """`wall`: the `now` given to step() is the current tick, other ticks are offset from it (the core
        has no clock of its own). `simulated`: epoch + tick * tick_s, repeatable but unrelated to real time."""
        if self.config.clock == "wall" and self._now is not None:
            t = datetime.fromtimestamp(self._now, timezone.utc) + timedelta(seconds=(tick - self.tick) * self.config.tick_s)
        else:
            t = self.config.epoch + timedelta(seconds=tick * self.config.tick_s)
        return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def emit(self, tick: int, kind: str, causal_id: str, **data: Any) -> None:
        self.events.append({"event_id": f"twin:e{len(self.events) + 1:06d}", "timestamp": self.timestamp(tick),
                            "tick": tick, "kind": kind, "causal_id": causal_id, "data": deepcopy(data)})

    # --- state ----------------------------------------------------------------------

    def reset(self, config: SimConfig | None = None) -> list[dict]:
        with self._lock:
            self.config = config or self.config
            self.tick, self._now = 0, None
            self._rng = Random(self.config.seed)
            self.city = self.config.city
            self.sensors, self.actuators, self.units = self.city.sensors, self.city.actuators, self.city.units
            self._scenarios: dict[str, tuple[str, int]] = {}      # zone_id -> (scenario, tick it started)
            self._triggered = False
            self._modes: dict[str, str] = {}
            self._call_count: dict[str, int] = {}
            self._injected: list[tuple[str, str, str]] = []
            self._truth: dict[str, float] = {}
            self._calls: list[dict] = []
            self.events: list[dict] = []
            self._devices = {d: {"device_id": d, "status": "active", "reason": "reset"}
                             for d in (*self.sensors, *self.actuators, *self.units)}
            self._act_state = {a: "default" for a in self.actuators}
            self._units = {u: {"status": "free", "zone_id": spec["home_zone"], "until": 0, "command": None,
                                "reserved_by": None} for u, spec in self.units.items()}
            self._commands: dict[str, dict] = {}
            self._effects: dict[str, dict] = {}
            self._flood_hold: dict[str, float] = {}    # zone_id -> level the flood stops rising at (valve open)
            self._pending_containment: list[tuple[int, dict]] = []
            self._delayed: list[tuple[int, dict]] = []
            self._history: dict[str, dict] = {}
            self._frozen: dict[tuple, Any] = {}
            self._last_seq: dict[str, int] = {}
            self.set_actuator_modes(self.config.actuator_modes)
            self.emit(0, "reset", self.config.run_id, seed=self.config.seed)
            return self.snapshot()

    def meta(self) -> dict[str, Any]:
        """City description (twin/meta)."""
        return {
            "zones": deepcopy(self.city.zones),
            "sensors": [{"sensor_id": sid, "zone_id": sp["zone_id"], "type": sp["type"], "unit": sp["unit"],
                         "min": sp["min"], "max": sp["max"], **({"alert": sp["alert"]} if sp["alert"] is not None else {})}
                        for sid, sp in self.sensors.items()],
            "actuators": [{"actuator_id": a, "type": sp["type"], "zone_id": sp["zone_id"],
                           "controls": list(sp["controls"]), "actions": list(sp["actions"])}
                          for a, sp in self.actuators.items()],
            "units": [{"unit_id": u, "type": sp["type"], "home_zone": sp["home_zone"]} for u, sp in self.units.items()],
        }

    def snapshot(self) -> list[dict]:
        """Retained states published after (re)connecting: actuator states and unit statuses."""
        with self._lock:
            out = [{"kind": "actuator", "actuator_id": a, "state": s} for a, s in self._act_state.items()]
            out += [self._unit_output(u) for u in self.units]
            return out

    def trigger(self, scenario: str, zone: str | None = None) -> None:
        """Start a scenario in a zone (default: `scenario_zone`, else the first zone). A zone has one scenario at
        a time, a new one replaces it; scenarios in different zones run side by side."""
        with self._lock:
            if scenario not in SCENARIOS:
                raise ValueError(f"unknown scenario {scenario!r}")
            zone = zone or self.config.scenario_zone or self.city.zone_ids[0]
            if zone not in self.city.zone_ids:
                raise ValueError(f"unknown zone {zone!r}")
            if scenario == "calm":
                self._scenarios.pop(zone, None)
            else:
                self._scenarios[zone] = (scenario, self.tick)
            self._triggered = True
            self.emit(self.tick, "scenario_trigger", self.config.run_id, scenario=scenario, zone_id=zone)

    def inject_call(self, zone: str, category: str, text: str) -> None:
        """An emergency call with this text, published in the next tick."""
        with self._lock:
            if zone not in self.city.zone_ids or category not in CALL_CATEGORIES or not isinstance(text, str):
                raise ValueError("inject_call needs a known zone, a known category and a text")
            self._injected.append((zone, category, text))
            self.emit(self.tick, "call_injected", self.config.run_id, zone_id=zone, category=category)

    def _make_call(self, zone: str, category: str, text: str | None = None) -> dict:
        """The n-th call of a tick gets the id suffix -n (n >= 2), so ids stay unique across zones and sources."""
        n = len(self._calls)
        k = self._call_count[category] = self._call_count.get(category, -1) + 1
        if text is None:
            texts = self.config.call_texts.get(category)
            text = texts[k % len(texts)].replace("{zone}", zone) if texts else f"{category} reported in {zone}"
        return {"kind": "call", "call_id": f"call-{self.config.run_id}-{self.tick:06d}" + (f"-{n + 1}" if n else ""),
                "zone_id": zone, "category": category, "text": text, "tick": self.tick}

    def _next_seq(self, sid: str) -> int:
        """Strictly increasing per sensor. Equals the tick, unless a `late` reading took a number meanwhile."""
        self._last_seq[sid] = seq = max(self.tick, self._last_seq.get(sid, 0) + 1)
        return seq

    def set_actuator_modes(self, modes: dict[str, str]) -> None:
        """Standing behaviour per actuator or unit (`ok` | `slow` | `no_ack` | `stuck` | `reject_quarantined`).
        Replaces the previous modes; commands already submitted keep the behaviour they were given."""
        with self._lock:
            for device, mode in modes.items():
                if device not in self.actuators and device not in self.units:
                    raise ValueError(f"unknown actuator or unit {device!r}")
                if mode not in ACTUATOR_MODES:
                    raise ValueError(f"unknown actuator mode {mode!r}")
            self._modes = dict(modes)
            self.emit(self.tick, "actuator_modes", self.config.run_id, modes=dict(modes))

    def _faults(self, device_id: str | None = None) -> list[Fault]:
        return [f for f in self.config.faults if f.matches(self.tick, device_id)]

    # --- commands -------------------------------------------------------------------

    def submit(self, command: dict[str, Any]) -> list[dict]:
        """Command from the Brain: {command_id, target_id, action, parameters}. Returns outputs."""
        with self._lock:
            cid = command["command_id"]
            payload = json.loads(json.dumps({k: command.get(k) for k in ("target_id", "action", "parameters")}))
            target = payload["target_id"]
            kind = "actuator" if target in self.actuators else "unit" if target in self.units else None
            if cid in self._commands:
                rec = self._commands[cid]
                if rec["payload"] != payload:
                    self.emit(self.tick, "command_conflict", cid)
                    return [self._ack(kind or "actuator", target, cid, "rejected", reason="command_id_reused")]
                self.emit(self.tick, "command_retry", cid)
                return [rec["ack"]] if rec["ack"] and "no_ack" not in rec["faults"] else []
            faults = {f.mode for f in self._faults(target)}
            mode = self._modes.get(target, "ok")
            if mode != "ok":
                faults.add("no_effect" if mode == "stuck" else mode)
            delay = self.config.command_delay_ticks
            if "slow" in faults:
                delay = max(delay, math.ceil(self.config.slow_ack_s / self.config.tick_s - 1e-9))
            rec = {"payload": payload, "kind": kind, "ack": None, "lost": False, "tick": self.tick,
                   "due": self.tick + delay, "state": "accepted", "faults": faults}
            self._commands[cid] = rec
            self.emit(self.tick, "command_submitted", cid, command=payload)
            problem = self._validate(kind, payload)
            if problem is None and self._devices[target]["status"] != "active":
                problem = self._unavailable(rec, target)
            if problem is None and "reject" in rec["faults"]:
                problem = "scripted_rejection"
            if problem is None and kind == "unit":
                u, action = self._units[target], payload["action"]
                if action == "dispatch" and (u["status"] != "free" or u["reserved_by"]):
                    problem = "unit_busy"       # also while another dispatch is accepted but not yet applied
                elif action == "recall" and u["status"] == "free":
                    problem = "unit_not_deployed"
                elif action == "dispatch":
                    u["reserved_by"] = cid      # held from now until the command is applied, failed or rejected
            if problem:
                rec["state"] = "rejected"
                return self._finish(cid, rec, "rejected", reason=problem)
            return []

    def _unavailable(self, rec: dict, target: str, at_application: bool = False) -> str:
        """Why a command to an isolated / quarantined device is refused."""
        if "reject_quarantined" in rec["faults"] and self._devices[target]["status"] == "quarantined":
            return "quarantined"
        return "device_unavailable_at_application" if at_application else "device_unavailable"

    def _validate(self, kind: str | None, p: dict) -> str | None:
        if kind is None:
            return "unknown_target"
        action, params = p["action"], p.get("parameters") or {}
        if not isinstance(params, dict):
            return "invalid_parameters"
        if kind == "actuator":
            if action not in self.actuators[p["target_id"]]["actions"]:
                return "unknown_action"
            if action == "open" and "opening" in params:
                o = params["opening"]
                if isinstance(o, bool) or not isinstance(o, (int, float)) or not 0 <= o <= 1:
                    return "invalid_parameters"
        else:
            if action not in UNIT_ACTIONS:
                return "unknown_action"
            if action == "dispatch" and params.get("zone_id", self.units[p["target_id"]]["home_zone"]) not in self.city.zone_ids:
                return "unknown_zone"
        return None

    def _ack(self, kind: str, target: str, cid: str, result: str, **extra: Any) -> dict:
        out = self._unit_output(target) if kind == "unit" else {"kind": "actuator", "actuator_id": target,
                                                                "state": self._act_state.get(target)}
        out.update(correlation_id=cid, result=result, **extra)
        return out

    def _finish(self, cid: str, rec: dict, result: str, **extra: Any) -> list[dict]:
        """Build the acknowledgement; a `lost_ack` fault drops the first one (the Brain must retry)."""
        ack = self._ack(rec["kind"] or "actuator", rec["payload"]["target_id"], cid, result, **extra)
        rec["ack"] = ack
        if "no_ack" in rec["faults"]:
            self.emit(self.tick, "acknowledgment_suppressed", cid)
            return []
        if "lost_ack" in rec["faults"] and not rec["lost"]:
            rec["lost"] = True
            self.emit(self.tick, "acknowledgment_lost", cid)
            return []
        return [ack]

    # --- containment (harness / Guardian-driven tests) -------------------------------------

    def contain(self, request: dict[str, Any]) -> None:
        """isolate / quarantine / release a device from the next tick on."""
        with self._lock:
            device, action = request["device_id"], request["action"]
            if device not in self._devices:
                raise ValueError("unknown device")
            if action not in ("isolate", "quarantine", "release"):
                raise ValueError("unknown containment action")
            if action == "isolate" and device not in self.sensors:
                raise ValueError("isolation requires a sensor")
            self._pending_containment.append((self.tick + 1, request))
            self.emit(self.tick, "containment_scheduled", request.get("request_id", device), request=request)

    # --- one tick -------------------------------------------------------------------

    def step(self, now: float | None = None) -> list[dict]:
        """Advance one tick. `now` (epoch seconds, from the caller's clock) stamps the tick in `wall` mode."""
        with self._lock:
            if now is not None:
                self._now = now
            # the configured scenario starts once, on the first step at or after its tick
            if not self._triggered and self.config.scenario != "calm" \
                    and self.tick >= self.config.scenario_after_ticks:
                self.trigger(self.config.scenario)
            self.tick += 1
            out: list[dict] = []
            for due, req in self._pending_containment[:]:
                if due <= self.tick:
                    state = {"isolate": "isolated", "quarantine": "quarantined", "release": "active"}[req["action"]]
                    self._devices[req["device_id"]].update(status=state, reason=req.get("reason", "containment"))
                    self._pending_containment.remove((due, req))
                    self.emit(self.tick, "containment_applied", req.get("request_id", req["device_id"]), device_id=req["device_id"], status=state)
            for cid, rec in self._commands.items():
                if rec["state"] == "accepted" and rec["due"] <= self.tick:
                    out += self._apply(cid, rec)
            out += self._advance_units()
            self._generate_truth()
            for due, reading in self._delayed[:]:
                if due <= self.tick:
                    out += self._deliver(reading)
                    self._delayed.remove((due, reading))
            for sid in self.sensors:
                out += self._sense(sid)
            out += self._calls
            self.emit(self.tick, "tick_completed", self.config.run_id)
            return out

    def _apply(self, cid: str, rec: dict) -> list[dict]:
        p = rec["payload"]
        target, action, params = p["target_id"], p["action"], p.get("parameters") or {}
        if rec["kind"] == "unit" and self._units[target]["reserved_by"] == cid:
            self._units[target]["reserved_by"] = None    # from here the unit's own status says it is busy
        if self._devices[target]["status"] != "active":
            rec["state"] = "failed"
            return self._finish(cid, rec, "failed", reason=self._unavailable(rec, target, at_application=True))
        if "failure" in rec["faults"]:
            rec["state"] = "failed"
            return self._finish(cid, rec, "failed", reason="scripted_actuator_failure")
        if rec["kind"] == "unit" and action == "recall" and self._units[target]["status"] == "free":
            rec["state"] = "failed"
            return self._finish(cid, rec, "failed", reason="unit_not_deployed")
        rec["state"] = "applied"
        effective = "no_effect" not in rec["faults"]
        if rec["kind"] == "actuator":
            if action == "open":
                self._act_state[target] = f"open_{round(float(params.get('opening', 1.0)) * 100)}"
            elif action == "close":
                self._act_state[target] = "closed"
            else:
                self._act_state[target] = action
            if effective:   # one effect per target: a new command replaces the old, a resting action ends it
                if action in _RESTING_ACTIONS:
                    self._effects.pop(target, None)
                else:
                    self._effects[target] = {"command_id": cid, "target": target, "action": action, "parameters": params}
        else:
            u = self._units[target]
            if action == "dispatch":
                u.update(status="en_route", zone_id=params.get("zone_id", self.units[target]["home_zone"]),
                         until=self.tick + self.config.travel_ticks, command=cid if effective else None)
            elif u["status"] in ("en_route", "on_scene"):   # already returning: nothing to do, the trip is not extended
                u.update(status="returning", until=self.tick + self.config.travel_ticks, command=None)
        self.emit(self.tick, "command_application", cid, effective=effective)
        return self._finish(cid, rec, "done")

    def _advance_units(self) -> list[dict]:
        out = []
        for uid, u in self._units.items():
            if u["status"] == "free" or u["until"] > self.tick:
                continue
            if u["status"] == "en_route":
                u.update(status="on_scene", until=self.tick + self.config.scene_ticks)
                if u["command"]:
                    self._effects[uid] = {"command_id": u["command"], "target": uid, "action": "dispatch", "parameters": {}}
            elif u["status"] == "on_scene":
                u.update(status="returning", until=self.tick + self.config.travel_ticks)
            else:
                u.update(status="free", zone_id=self.units[uid]["home_zone"], command=None)
                self._effects.pop(uid, None)
            self.emit(self.tick, "unit_state", uid, status=u["status"])
            out.append(self._unit_output(uid))
        return out

    def _unit_output(self, uid: str) -> dict:
        u = self._units[uid]
        return {"kind": "unit", "unit_id": uid, "status": u["status"], "zone_id": u["zone_id"]}

    # --- readings -------------------------------------------------------------------

    def _sense(self, sid: str) -> list[dict]:
        spec = self.sensors[sid]
        if self._devices[sid]["status"] != "active":
            self.emit(self.tick, "reading_suppressed", sid, reason="device_unavailable")
            return []
        rid = f"{self.config.run_id}-{sid}-{self.tick:06d}"
        faults = self._faults(sid)
        late = any(f.mode == "late" for f in faults)   # `late` takes its sequence on delivery, not now
        reading = {"reading_id": rid, "sensor_id": sid, "zone_id": spec["zone_id"], "type": spec["type"], "value": self._truth[sid],
                   "unit": spec["unit"], "origin": "simulated", "tick": self.tick,
                   "sequence": None if late else self._next_seq(sid), "observed_at": self.timestamp(self.tick)}
        clean = deepcopy(reading)   # kept for freeze / replay of later ticks, which must not reuse corrupted values
        dropped, delay, duplicate, tamper = False, None, False, []
        for fault in faults:
            if fault.mode in _NOT_READING_FAULTS:
                continue
            self.emit(self.tick, "reading_fault", rid, mode=fault.mode, sensor_id=sid)
            if fault.mode == "malformed": reading.pop("value", None)
            elif fault.mode == "impossible": reading["value"] = -999.0
            elif fault.mode == "wrong_type": reading["value"] = "not-a-number"
            elif fault.mode == "wrong_unit": reading["unit"] = "bananas"
            elif fault.mode == "falsify": reading["value"] = fault.value
            elif fault.mode == "unknown_id": reading["sensor_id"] = "ghost-01"
            elif fault.mode == "bad_hmac": tamper.append("bad_hmac")
            elif fault.mode == "missing": dropped = True
            elif fault.mode == "freeze":
                key = (sid, fault.tick)
                if key not in self._frozen:
                    self._frozen[key] = self._history.get(sid, reading).get("value")
                reading["value"] = self._frozen[key]
            elif fault.mode == "duplicate": duplicate = True
            elif fault.mode == "old":
                old = max(0, self.tick - fault.delay_ticks)
                reading.update(observed_at=self.timestamp(old), sequence=old)
            elif fault.mode == "replay":
                if sid in self._history: reading = deepcopy(self._history[sid])
                else: dropped = True
            elif fault.mode in ("delay", "late"): delay = fault.delay_ticks
        self._history[sid] = clean
        item = {"kind": "reading", "reading": reading, "tamper": tamper}
        if dropped:
            self.emit(self.tick, "reading_dropped", rid)
            return []
        if delay:
            self._delayed.append((self.tick + delay, item))
            if duplicate: self._delayed.append((self.tick + delay, deepcopy(item)))
            self.emit(self.tick, "reading_delivery_scheduled", rid, due_tick=self.tick + delay)
            return []
        return self._deliver(item) * (2 if duplicate else 1)

    def _deliver(self, item: dict) -> list[dict]:
        sid = item["reading"]["sensor_id"]
        if sid in self._devices and self._devices[sid]["status"] != "active":
            self.emit(self.tick, "reading_suppressed", item["reading"]["reading_id"], reason="contained_before_delivery")
            return []
        if item["reading"]["sequence"] is None:
            item["reading"]["sequence"] = self._next_seq(sid)
        self.emit(self.tick, "reading_delivered", item["reading"]["reading_id"], applied_command_ids=[e["command_id"] for e in self._effects.values()])
        return [deepcopy(item)]

    def _rush_factor(self) -> float:
        """0 outside the rush hour, a half sine up to 1 at its middle."""
        r = self.config.rush_hour
        if not r or not r["start_tick"] <= self.tick <= r["end_tick"]:
            return 0.0
        return math.sin(math.pi * (self.tick - r["start_tick"]) / (r["end_tick"] - r["start_tick"]))

    def _generate_truth(self) -> None:
        def jitter(base: float, absolute: float = 0.0) -> float:
            v = base * (1 + self._rng.uniform(-self.config.noise, self.config.noise))
            if absolute:
                v += self._rng.uniform(-absolute, absolute)
            return round(v, 3)

        # calm values per zone and sensor type, then the zone's scenario, then the effects of commands
        zone_values = {z: {sp["type"]: sp["baseline"] for sp in self.sensors.values() if sp["zone_id"] == z}
                       for z in self.city.zone_ids}
        self._calls = []
        rush = self._rush_factor()
        for zone in self.city.zone_ids:
            v = zone_values[zone]
            if rush:
                r = self.config.rush_hour
                v["traffic_speed"] *= 1 - r["speed_drop"] * rush
                v["traffic_density"] *= 1 + r["density_rise"] * rush
                v["traffic_flow"] *= 1 + r["flow_rise"] * rush
            scenario, started = self._scenarios.get(zone, ("calm", 0))
            age = self.tick - started

            def call(category: str, zone: str = zone) -> dict:     # bound now, not at call time
                return self._make_call(zone, category)

            if scenario == "flood":
                level = min(2.5, 1.4 + age * .15)
                if any(e["action"] == "open" and e["target"] in self.actuators
                       and zone in self.actuators[e["target"]]["controls"] for e in self._effects.values()):
                    # an open valve holds the flood at the level of the previous tick (the offset applied
                    # below then lowers it): the level no longer keeps rising past the valve's effect
                    level = min(level, self._flood_hold.setdefault(zone, min(2.5, 1.4 + (age - 1) * .15)))
                else:
                    self._flood_hold.pop(zone, None)
                v["water_level"] = level
                if age >= 3 and (age - 3) % 5 == 0:     # residents report it, every 5 ticks from tick 3
                    self._calls.append(call("flood"))
            elif scenario == "fire":
                v["air_quality"], v["power_load"] = 250., 650.
                _add_trace(v, "fire", age)             # smoke, CO, CO2, heat of a real fire (if sensed here)
                self._calls.append(call("fire"))
            elif scenario == "cooking":                  # burnt toast: as much smoke as the fire, no CO, no call
                v["air_quality"] = 250.
                _add_trace(v, "cooking", age)
            elif scenario == "accident" and age >= 2:
                v["traffic_speed"], v["traffic_density"], v["traffic_flow"] = 5., 160., 2.
                self._calls.append(call("accident"))
            elif scenario == "traffic-jam":
                v["traffic_speed"], v["traffic_density"], v["traffic_flow"] = 7., 180., 4.
        for c in self.config.calls:
            if c["tick"] <= self.tick <= c["end_tick"]:
                for _ in range(c["count"]):
                    self._calls.append(self._make_call(c["zone_id"], c["category"], c["text"]))
        for zone, category, text in self._injected:
            self._calls.append(self._make_call(zone, category, text))
        self._injected = []
        for e in self._effects.values():
            t, a, p = e["target"], e["action"], e["parameters"]
            zones = self.actuators[t]["controls"] if t in self.actuators else [self._units[t]["zone_id"]]
            for z in zones:
                v = zone_values[z]
                if t in self.actuators and a == "open": v["water_level"] = max(.1, v["water_level"] - .3 * float(p.get("opening", 1.0)))
                elif t in self.actuators and a == "priority": v["traffic_speed"] += 8.; v["traffic_flow"] += 5.
                elif t in self.actuators and a == "shed": v["power_load"] *= .85
                elif t in self.units and self.units[t]["type"] == "fire_truck": v["air_quality"] *= .7
                elif t in self.units: v["traffic_speed"] += 5.; v["traffic_flow"] += 3.
        self._truth = {sid: _clamp(jitter(zone_values[sp["zone_id"]][sp["type"]], sp.get("noise_abs", 0.0)),
                                   sp["min"], sp["max"])
                       for sid, sp in self.sensors.items()}
