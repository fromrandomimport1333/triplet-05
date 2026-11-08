"""Guardian mock core: trust verdicts on sensor readings. No MQTT, no clock, no signing.

Every call takes `now` (epoch seconds) from the caller and returns the list of *outputs* the transport
must publish. Each output is a plain dict with a `kind` (`verdict`, `quarantine`, `status`);
`service.py` turns them into sealed wire messages.

Checks (mode `auto`), first match wins:

| Check | Verdict | Reason |
|---|---|---|
| value outside the sensor's [min, max] | rejected | `out_of_range` |
| change since the last trusted value above `max_step` for the type (per tick) | rejected | `implausible_jump` |
| the same value `frozen_rejected_n` times in a row | rejected | `frozen` |
| pinned at the min or max bound `flatline_n` times in a row | rejected | `flatline` |
| the same value `frozen_suspicious_n` times in a row | suspicious | `frozen` |
| otherwise | trusted | `consistent` (lower score when far from the sensor's own baseline) |

A jump stays rejected while the value stays away from the last trusted level, unless the sensor
settles at the new level with live noise for `rebase_n` readings (a real step, not a forged constant).

Forced modes (team plan 06, section 5) override the checks; the checks keep running underneath, so
going back to `auto` is seamless.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

VERDICTS = ("trusted", "suspicious", "rejected")
SENSOR_MODES = ("reject", "suspicious", "flip_flop", "clear")   # <mode>:<sensor_id>
DEVICE_MODES = ("quarantine", "release")                         # <mode>:<device_id>
GLOBAL_MODES = ("auto", "trust_all", "slow", "fast", "offline", "online", "flip_flop")


def parse_mode(spec: str) -> tuple[str, str | None]:
    """`reject:water-01` -> ("reject", "water-01"); `trust_all` -> ("trust_all", None). Raises ValueError."""
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"mode must be a non-empty string, not {spec!r}")
    name, _, target = spec.partition(":")
    if target:
        if name not in SENSOR_MODES + DEVICE_MODES:
            raise ValueError(f"mode {name!r} takes no target")
        return name, target
    if name not in GLOBAL_MODES:
        raise ValueError(f"unknown Guardian mode {spec!r}")
    return name, None


class SensorState:
    """What the checks remember about one sensor."""

    def __init__(self, window: int):
        self.values: deque[float] = deque(maxlen=window)
        self.ref: float | None = None          # last trusted value (jump reference)
        self.last_t: float | None = None
        self.last_seq: int | None = None
        self.since_jump: list[float] = []      # values seen since a rejected jump (rebase candidates)
        self.mean: float | None = None         # baseline (EWMA of trusted values)
        self.var = 0.0
        self.n = 0
        self.trusted_run = 0                   # trusted readings in a row (automatic release)
        self.flips = 0
        self.correlation_id: str | None = None


class GuardianCore:
    def __init__(self, cfg: dict[str, Any], sensors: dict[str, dict[str, Any]], devices: set[str] | None = None):
        """cfg: the guardian-mock.yaml content. sensors: sensor_id -> {type, min, max}."""
        self.cfg = cfg
        self.checks = cfg.get("checks", {})
        self.trust = {"trusted": 0.95, "trust_all": 0.95, "suspicious": 0.5, "rejected": 0.05,
                      "baseline_penalty": 0.05, **(cfg.get("trust") or {})}
        self.refresh_s = float(cfg.get("refresh_s", 5))
        self.slow_s = float(cfg.get("slow_s", 5))
        self.tick_s = float(cfg.get("tick_s", 1))
        self.isolate_on_reject = bool(cfg.get("isolate_on_reject", True))
        self.isolate_reasons = set(cfg.get("isolate_reasons", ["implausible_jump"]))
        self.auto_release_after = int(cfg.get("auto_release_after", 5))
        self.forced_reasons = {"reject": "out_of_range", "suspicious": "inconsistent", "quarantine": "forced",
                               "release": "forced", **(cfg.get("forced_reasons") or {})}
        self.set_city(sensors, devices)
        self.reset()

    # --- city -----------------------------------------------------------------------

    def set_city(self, sensors: dict[str, dict[str, Any]], devices: set[str] | None = None) -> None:
        self.sensors = {sid: {"type": s["type"], "min": float(s["min"]), "max": float(s["max"])}
                        for sid, s in sensors.items()}
        self.devices = set(self.sensors) | set(devices or ())

    @staticmethod
    def sensors_from_meta(meta: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
        sensors = {s["sensor_id"]: s for s in meta.get("sensors", [])}
        devices = {a["actuator_id"] for a in meta.get("actuators", [])} | {u["unit_id"] for u in meta.get("units", [])}
        return sensors, devices

    # --- modes ----------------------------------------------------------------------

    def reset(self) -> None:
        """Forget everything; back to the modes of the config. Quarantines are released by the caller."""
        self.base = "auto"                       # auto | trust_all
        self.overrides: dict[str, str] = {}      # sensor_id or "*" -> reject | suspicious | flip_flop
        self.slow = False
        self.offline = False
        self.state: dict[str, SensorState] = {}
        self.auto: dict[str, dict[str, Any]] = {}         # sensor_id -> last verdict of the checks
        self.verdicts: dict[str, dict[str, Any]] = {}     # sensor_id -> effective verdict (after forced modes)
        self.published: dict[str, tuple[str, str, float]] = {}   # sensor_id -> (verdict, reason, at)
        self.quarantined: dict[str, str] = {}    # device_id -> reason
        self.auto_isolated: set[str] = set()
        self._queue: list[tuple[float, dict[str, Any]]] = []      # slow mode
        self._seen: deque[str] = deque(maxlen=4096)
        self._seen_set: set[str] = set()

    def apply_modes(self, specs: list[str], now: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for spec in specs:
            out += self.apply_mode(spec, now)
        return out

    def apply_mode(self, spec: str, now: float) -> list[dict[str, Any]]:
        name, target = parse_mode(spec)
        out: list[dict[str, Any]] = []
        if name in DEVICE_MODES:
            if target not in self.devices:
                raise ValueError(f"unknown device {target!r}")
            if name == "quarantine":
                out += self._quarantine(target, self.forced_reasons["quarantine"])
            else:
                out += self._release(target, self.forced_reasons["release"])
            return self._emit(out, now)
        if name in SENSOR_MODES:
            if target not in self.sensors:
                raise ValueError(f"unknown sensor {target!r}")
            if name == "clear":
                self.overrides.pop(target, None)
            else:
                self.overrides[target] = name
            return self._emit(self._recompute([target]), now)
        if name in ("auto", "trust_all"):
            self.base = name
            self.overrides.clear()
            if name == "auto":
                self.slow = False
            out += self._recompute(list(self.verdicts))
        elif name == "flip_flop":
            self.overrides["*"] = "flip_flop"
        elif name in ("slow", "fast"):
            self.slow = name == "slow"
        elif name == "offline":
            if not self.offline:
                self.offline = True
                self._queue.clear()
                return [{"kind": "status", "status": "offline"}]   # the last thing said before going silent
            return []
        elif name == "online":
            return self.go_online(now)
        return self._emit(out, now)

    def go_online(self, now: float) -> list[dict[str, Any]]:
        """Leave the offline mode: status online, then every current verdict at once."""
        self.offline = False
        out = [{"kind": "status", "status": "online"}]
        out += [self._mark(sid, now) for sid in self.verdicts]
        return out

    # --- readings -------------------------------------------------------------------

    def observe(self, reading: dict[str, Any], now: float) -> list[dict[str, Any]]:
        """One reading body (as the Twin sends it). Unknown sensors, bad values and repeats are ignored."""
        sid, value, rid = reading.get("sensor_id"), reading.get("value"), reading.get("id")
        if sid not in self.sensors or isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or not isinstance(rid, str):
            return []
        if rid in self._seen_set:
            return []
        self._remember(rid)
        st = self.state.setdefault(sid, SensorState(self._window()))
        seq = reading.get("seq")
        if isinstance(seq, int) and st.last_seq is not None and seq <= st.last_seq:
            return []                            # a replayed or reordered reading says nothing new
        if isinstance(seq, int):
            st.last_seq = seq
        st.correlation_id = rid
        auto = self._check(sid, st, float(value), now)
        self.auto[sid] = auto
        verdict = self._override(sid, st, auto)
        verdict["correlation_id"] = rid
        self.verdicts[sid] = verdict
        out = self._isolation(sid, auto)
        prev = self.published.get(sid)
        if prev is None or (prev[0], prev[1]) != (verdict["verdict"], verdict["reason"]):
            out.append(self._mark(sid, now))
        return self._emit(out, now)

    def tick(self, now: float) -> list[dict[str, Any]]:
        """Called every second: refresh old verdicts (so the Brain never thinks Guardian is silent),
        release the slow queue."""
        if self.offline:
            return []
        out = [self._mark(sid, now) for sid, (_, _, at) in list(self.published.items())
               if now - at >= self.refresh_s and sid in self.verdicts]
        out = self._emit(out, now)
        due = [o for t, o in self._queue if t <= now]
        self._queue = [(t, o) for t, o in self._queue if t > now]
        return out + due

    # --- internals ------------------------------------------------------------------

    def _window(self) -> int:
        c = self.checks
        return max(int(c.get("frozen_rejected_n", 10)), int(c.get("flatline_n", 5)), 2)

    def _remember(self, rid: str) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(rid)
        self._seen_set.add(rid)

    def _verdict(self, verdict: str, reason: str, score: float, details: str | None = None) -> dict[str, Any]:
        v = {"kind": "verdict", "verdict": verdict, "reason": reason, "trust_score": round(max(0.0, min(1.0, score)), 3)}
        if details:
            v["details"] = details
        return v

    def _check(self, sid: str, st: SensorState, v: float, now: float) -> dict[str, Any]:
        spec, c, t = self.sensors[sid], self.checks, self.trust
        dt = (now - st.last_t) if st.last_t is not None else self.tick_s
        st.last_t = now
        if not spec["min"] <= v <= spec["max"]:
            return self._verdict("rejected", "out_of_range", t["rejected"],
                                 f"{v:g} outside [{spec['min']:g}, {spec['max']:g}]")
        st.values.append(v)
        step_max = (c.get("max_step") or {}).get(spec["type"])
        if isinstance(step_max, dict) and st.ref is not None:    # {rise, fall}: a level may drop faster than it rises
            step_max = step_max.get("rise" if v >= st.ref else "fall")
        if st.ref is not None and step_max is not None:
            allowed = float(step_max) * max(1.0, dt / self.tick_s)
            if abs(v - st.ref) > allowed:
                if self._rebased(st, v, allowed):
                    detail = f"new level {v:g} accepted after {len(st.since_jump)} consistent readings"
                    st.since_jump, st.ref = [], v
                    return self._trusted(st, v, detail)
                return self._verdict("rejected", "implausible_jump", t["rejected"],
                                     f"{st.ref:g} -> {v:g} ({spec['type']}), more than {allowed:g} per tick")
        st.since_jump = []
        vals = list(st.values)
        fr, fs, fl = int(c.get("frozen_rejected_n", 10)), int(c.get("frozen_suspicious_n", 5)), int(c.get("flatline_n", 5))
        if _same_tail(vals, fr):
            return self._verdict("rejected", "frozen", t["rejected"], f"same value {v:g} for {fr} readings")
        if len(vals) >= fl and all(x in (spec["min"], spec["max"]) for x in vals[-fl:]):
            return self._verdict("rejected", "flatline", t["rejected"], f"pinned at {v:g} for {fl} readings")
        if _same_tail(vals, fs):
            return self._verdict("suspicious", "frozen", t["suspicious"], f"same value {v:g} for {fs} readings")
        st.ref = v
        return self._trusted(st, v)

    def _rebased(self, st: SensorState, v: float, allowed: float) -> bool:
        """After a rejected jump: accept the new level once it held for rebase_n readings with live noise."""
        n = int(self.checks.get("rebase_n", 5))
        if st.since_jump and abs(v - st.since_jump[-1]) > allowed:
            st.since_jump = []
        st.since_jump.append(v)
        recent = st.since_jump[-n:]
        return n > 0 and len(recent) >= n and len(set(recent)) > 1

    def _trusted(self, st: SensorState, v: float, details: str | None = None) -> dict[str, Any]:
        c, score = self.checks, self.trust["trusted"]
        z_max, min_n, alpha = float(c.get("baseline_z", 6)), int(c.get("baseline_min_n", 10)), float(c.get("baseline_alpha", 0.05))
        if st.mean is None:
            st.mean, st.var = v, 0.0
        else:
            sd = math.sqrt(st.var)
            scale = max(sd, 0.01 * max(abs(st.mean), 1e-6))
            if st.n >= min_n and abs(v - st.mean) / scale > z_max:
                score -= self.trust["baseline_penalty"]
                details = details or f"far from baseline {st.mean:.3g}"
            diff = v - st.mean
            st.mean += alpha * diff
            st.var = (1 - alpha) * (st.var + alpha * diff * diff)
        st.n += 1
        return self._verdict("trusted", "consistent", score, details)

    def _override(self, sid: str, st: SensorState, auto: dict[str, Any]) -> dict[str, Any]:
        mode = self.overrides.get(sid) or self.overrides.get("*")
        t = self.trust
        if mode == "reject":
            return self._verdict("rejected", self.forced_reasons["reject"], t["rejected"], "forced by test control")
        if mode == "suspicious":
            return self._verdict("suspicious", self.forced_reasons["suspicious"], t["suspicious"], "forced by test control")
        if mode == "flip_flop":
            st.flips += 1
            if st.flips % 2:
                return self._verdict("rejected", "flip_flop", t["rejected"], "alternating verdicts (robustness test)")
            return self._verdict("trusted", "flip_flop", t["trusted"], "alternating verdicts (robustness test)")
        if self.base == "trust_all":
            return self._verdict("trusted", "trust_all", t["trust_all"])
        return dict(auto)

    def _recompute(self, sids: list[str]) -> list[dict[str, Any]]:
        """Forced modes changed: verdicts that change are published at once (without waiting for a reading)."""
        out = []
        for sid in sids:
            if sid not in self.auto:
                continue
            old = self.verdicts[sid]
            new = self._override(sid, self.state[sid], self.auto[sid])
            new["correlation_id"] = self.state[sid].correlation_id
            self.verdicts[sid] = new
            if (new["verdict"], new["reason"]) != (old["verdict"], old["reason"]):
                out.append({**new, "sensor_id": sid})
        return out

    def _isolation(self, sid: str, auto: dict[str, Any]) -> list[dict[str, Any]]:
        """isolate_on_reject: quarantine notice for a sensor rejected by the checks; automatic release."""
        st = self.state[sid]
        if auto["verdict"] == "trusted":
            st.trusted_run += 1
        else:
            st.trusted_run = 0
        if not self.isolate_on_reject:
            return []
        if auto["verdict"] == "rejected" and auto["reason"] in self.isolate_reasons and sid not in self.quarantined:
            self.auto_isolated.add(sid)
            return self._quarantine(sid, auto["reason"])
        if sid in self.auto_isolated and self.auto_release_after and st.trusted_run >= self.auto_release_after:
            self.auto_isolated.discard(sid)
            return self._release(sid, "consistent_again")
        return []

    def _quarantine(self, device: str, reason: str) -> list[dict[str, Any]]:
        self.quarantined[device] = reason
        return [{"kind": "quarantine", "device_id": device, "state": "quarantined", "reason": reason}]

    def _release(self, device: str, reason: str) -> list[dict[str, Any]]:
        self.auto_isolated.discard(device)
        self.quarantined.pop(device, None)
        return [{"kind": "quarantine", "device_id": device, "state": "released", "reason": reason}]

    def release_all(self) -> list[dict[str, Any]]:
        out = []
        for device in list(self.quarantined):
            out += self._release(device, "reset")
        return out

    def _mark(self, sid: str, now: float) -> dict[str, Any]:
        v = self.verdicts[sid]
        self.published[sid] = (v["verdict"], v["reason"], now)
        return {**v, "sensor_id": sid}

    def _emit(self, out: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Offline: nothing goes out. Slow: everything goes out slow_s later (stamped when it leaves)."""
        for o in out:
            if o["kind"] == "verdict":
                self.published[o["sensor_id"]] = (o["verdict"], o["reason"], now)
        if self.offline:
            return []
        if self.slow:
            self._queue += [(now + self.slow_s, o) for o in out]
            return []
        return out


def _same_tail(values: list[float], n: int) -> bool:
    return n > 1 and len(values) >= n and len(set(values[-n:])) == 1
