"""Effect check (team plan 05, section 5 point 5): did the acknowledged command change the city?

When a command with a configured check is SENT, every sensor of the expected metric in the target
zones is watched, starting from its last value. The effect is seen as soon as one reading is at
least `margin` past the extreme reached since the command was sent:

- falls: reading <= highest value since sending - margin   (the level turned down)
- rises: reading >= lowest value since sending + margin    (e.g. traffic speed went up)

margin = max(min_delta, min_rel x |value at sending|), set above the sensor noise.
No such reading within `window_s` of the acknowledgement = no effect (stuck valve, scenario B6).

Why the running extreme and not a projected trend: a flood that levels off on its own would
look like "stopped rising" against a projected trend, and a stuck valve would pass. A turn
past the peak only happens when something lowers the level.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EffectCheck:
    command_id: str
    metric: str
    expect: str                         # falls | rises
    zones: list[str]
    min_delta: float
    min_rel: float
    sent_at: float
    deadline: float = 0.0
    armed_at: float | None = None       # time of the acknowledgement; None = not acked yet
    start: dict[str, float] = field(default_factory=dict)   # sensor_id -> value when sent
    seen: dict[str, Any] | None = None  # the reading that showed the effect


class EffectTracker:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.window_s = float(cfg.get("window_s", 30))
        self.specs = list(cfg.get("checks", []))
        self._history: dict[str, deque[tuple[float, float]]] = {}     # sensor_id -> (t, value)
        self._sensor: dict[str, tuple[str, str]] = {}                 # sensor_id -> (zone, type)
        self.checks: dict[str, EffectCheck] = {}
        # keep enough history for the slowest ack (timeout + retry) plus the window
        self.keep_s = float(cfg.get("history_s", self.window_s + 30))

    # --- readings -----------------------------------------------------------------------

    def add_reading(self, sensor_id: str, zone_id: str, metric: str, value: float, now: float) -> None:
        self._sensor[sensor_id] = (zone_id, metric)
        h = self._history.setdefault(sensor_id, deque())
        h.append((now, float(value)))
        while h and now - h[0][0] > self.keep_s:
            h.popleft()

    # --- checks -------------------------------------------------------------------------

    def spec_for(self, kind: str, dev_type: str, action: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        for s in self.specs:
            if s.get("action") == action and s.get("kind", kind) == kind and s.get("type") in (None, dev_type):
                return s
        return None

    def start(self, command_id: str, spec: dict[str, Any], zones: list[str], now: float) -> EffectCheck | None:
        """Note the starting values (call when the command is sent). None if no sensor to watch."""
        check = EffectCheck(command_id=command_id, metric=spec["metric"], expect=spec.get("expect", "falls"),
                            zones=list(zones), min_delta=float(spec.get("min_delta", 0.0)),
                            min_rel=float(spec.get("min_rel", 0.0)), sent_at=now)
        for sid, (zone, typ) in self._sensor.items():
            h = self._history.get(sid)
            if typ == check.metric and zone in check.zones and h:
                check.start[sid] = h[-1][1]
        if not check.start:
            return None
        self.checks[command_id] = check
        return check

    def arm(self, command_id: str, now: float) -> EffectCheck | None:
        """The command was acknowledged `done`: the window starts."""
        c = self.checks.get(command_id)
        if c is not None:
            c.armed_at = now
            c.deadline = now + self.window_s
        return c

    def cancel(self, command_id: str) -> None:
        self.checks.pop(command_id, None)

    def _seen(self, c: EffectCheck) -> dict[str, Any] | None:
        for sid, v0 in c.start.items():
            margin = max(c.min_delta, c.min_rel * abs(v0))
            extreme = v0
            for t, v in self._history.get(sid, ()):
                if t < c.sent_at:
                    continue
                if c.expect == "falls":
                    if v <= extreme - margin:
                        return {"sensor_id": sid, "value": v, "from": extreme}
                    extreme = max(extreme, v)
                else:
                    if v >= extreme + margin:
                        return {"sensor_id": sid, "value": v, "from": extreme}
                    extreme = min(extreme, v)
        return None

    def evaluate(self, now: float) -> tuple[list[EffectCheck], list[EffectCheck]]:
        """(checks whose effect was seen, checks that timed out). Both are removed."""
        ok: list[EffectCheck] = []
        failed: list[EffectCheck] = []
        for cid, c in list(self.checks.items()):
            if c.armed_at is None:
                continue
            c.seen = self._seen(c)
            if c.seen is not None:
                ok.append(c)
                del self.checks[cid]
            elif now >= c.deadline:
                failed.append(c)
                del self.checks[cid]
        return ok, failed
