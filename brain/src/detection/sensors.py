"""Step 1 of detection: is one sensor abnormal? (team doc 02-graph, 4.1)

Per sensor, a running mean and variance (cumulative during warm-up, then an EWMA with
weight `smoothing`). A reading is "bad" when it is at or beyond the sensor's alert level
from the city description, or (after warm-up) more than z_far standard deviations away
in the bad direction of its type. `consecutive` bad readings in a row make the sensor
abnormal; `consecutive` good readings (below z_clear and below alert x alert_clear_ratio)
make it normal again. The baseline is frozen while the sensor is abnormal or a reading is
bad, so a long flood is never learned as normal. After warm-up only readings within
z_clear (both directions) are learned, so a slow ramp (rush hour) cannot inflate the
variance and hide itself; readings kept out of the baseline for relearn_after_s in a row
(and not at alert level) are learned again, so a permanent shift is eventually accepted.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SensorState:
    sensor_id: str
    zone_id: str
    type: str
    unit: str
    alert: float | None
    direction: str                     # up | down | either
    n: int = 0                         # readings learned
    mean: float = 0.0
    var: float = 0.0
    m2: float = 0.0                    # Welford sum during warm-up
    bad_streak: int = 0
    good_streak: int = 0
    alert_streak: int = 0
    abnormal: bool = False
    abnormal_since: float | None = None
    recovered_at: float | None = None  # last time it went from abnormal back to normal
    onset_ts: float | None = None      # first bad reading of the current episode
    onset_corr: str | None = None      # its correlation_id (chain start)
    sudden: bool = False
    z: float = 0.0                     # latest z in the bad direction
    skip_since: float | None = None    # first of the readings kept out of the baseline in a row
    last_ts: float | None = None
    last_value: float | None = None
    last_corr: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=12))

    def reset_episode(self) -> None:
        """Forget the current abnormal episode (the Twin restarted); the learned baseline stays."""
        self.bad_streak = self.good_streak = self.alert_streak = 0
        self.abnormal, self.abnormal_since, self.recovered_at = False, None, None
        self.onset_ts = self.onset_corr = None
        self.sudden = False
        self.skip_since = None
        self.history.clear()

    # --- helpers --------------------------------------------------------------------

    def std(self, cfg: dict[str, Any]) -> float:
        return max(math.sqrt(max(self.var, 0.0)), float(cfg["min_rel_std"]) * abs(self.mean),
                   float(cfg["min_abs_std"]))

    def warm(self, cfg: dict[str, Any]) -> bool:
        return self.n >= int(cfg["warmup_readings"])

    def _bad_side(self, delta: float) -> float:
        """Deviation (in std units or unit values) projected on the bad direction."""
        if self.direction == "up":
            return delta
        if self.direction == "down":
            return -delta
        return abs(delta)

    def at_alert(self, value: float, ratio: float = 1.0) -> bool:
        if self.alert is None:
            return False
        if self.direction == "down":
            return value <= self.alert / ratio
        return value >= self.alert * ratio

    def stale(self, now: float, stale_s: float) -> bool:
        return self.last_ts is None or now - self.last_ts > stale_s

    def margin(self) -> float:
        """(value - alert) / alert in the bad direction, 0 when not at alert level."""
        if self.alert is None or self.last_value is None or not self.alert:
            return 0.0
        m = self._bad_side(self.last_value - self.alert) / abs(self.alert)
        return max(0.0, m)

    def strength(self, cfg: dict[str, Any]) -> float:
        """1.0 at alert level, else min(1, z / z_full)."""
        if self.last_value is not None and self.at_alert(self.last_value):
            return 1.0
        return max(0.0, min(1.0, self.z / float(cfg["z_full"])))

    def alert_sustained(self, cfg: dict[str, Any]) -> bool:
        return self.alert_streak >= int(cfg["consecutive"])

    def drop_frac(self) -> float:
        """Deviation of the latest value from the baseline, in the bad direction, as a fraction of it."""
        if self.last_value is None or not self.mean:
            return 0.0
        return self._bad_side(self.last_value - self.mean) / abs(self.mean)

    def rising(self, cfg: dict[str, Any]) -> bool:
        window = int(cfg["rising"]["window"])
        values = [v for _, v in list(self.history)[-window:]]
        if len(values) < 2:
            return False
        scale = max(abs(values[0]), abs(self.alert or 0.0))
        return self._bad_side(values[-1] - values[0]) > float(cfg["rising"]["min_rise_frac"]) * scale

    # --- one reading ------------------------------------------------------------------

    def update(self, value: float, ts: float, corr: str | None, cfg: dict[str, Any],
               learn: bool = True) -> bool:
        """Apply one reading. Returns True when the abnormal state flipped."""
        n_req = int(cfg["consecutive"])
        std = self.std(cfg)
        warm = self.warm(cfg)
        z_signed = (value - self.mean) / std if self.n else 0.0
        self.z = self._bad_side(z_signed)
        at_alert = self.at_alert(value)
        bad = at_alert or (warm and self.z > float(cfg["z_far"]))
        good = not self.at_alert(value, float(cfg["alert_clear_ratio"])) and not (
            warm and self.z > float(cfg["z_clear"]))

        self.last_ts, self.last_value, self.last_corr = ts, value, corr
        self.history.append((ts, value))
        self.alert_streak = self.alert_streak + 1 if at_alert else 0
        if bad:
            self.bad_streak += 1
            if self.bad_streak == 1 and not self.abnormal:
                self.onset_ts, self.onset_corr, self.sudden = ts, corr, False
        else:
            self.bad_streak = 0
        self.good_streak = self.good_streak + 1 if good else 0

        if (bad or self.abnormal) and not self.sudden and self.onset_ts is not None and self.n:
            far = self._bad_side(value - self.mean) >= float(cfg["sudden_drop"]["drop_frac"]) * abs(self.mean)
            if far and ts - self.onset_ts <= float(cfg["sudden_drop"]["within_s"]):
                self.sudden = True

        changed = False
        if not self.abnormal and self.bad_streak >= n_req:
            self.abnormal, self.abnormal_since, changed = True, ts, True
        elif self.abnormal and self.good_streak >= n_req:
            self.abnormal, self.abnormal_since, changed = False, None, True
            self.recovered_at = ts
            self.onset_ts = self.onset_corr = None
            self.sudden = False
        elif not self.abnormal and not bad:
            self.onset_ts = self.onset_corr = None
            self.sudden = False

        quiet = not bad and not self.abnormal and (not warm or abs(z_signed) <= float(cfg["z_clear"]))
        if quiet:
            self.skip_since = None
        else:
            self.skip_since = self.skip_since if self.skip_since is not None else ts
        relearn = (not at_alert and self.skip_since is not None
                   and ts - self.skip_since > float(cfg["relearn_after_s"]))
        if learn and (quiet or relearn):
            self._learn(value, cfg)
        return changed

    def _learn(self, x: float, cfg: dict[str, Any]) -> None:
        self.n += 1
        if self.n == 1:
            self.mean, self.var, self.m2 = x, 0.0, 0.0
        elif self.n <= int(cfg["warmup_readings"]):
            delta = x - self.mean
            self.mean += delta / self.n
            self.m2 += delta * (x - self.mean)
            self.var = self.m2 / self.n
        else:
            alpha = float(cfg["smoothing"])
            diff = x - self.mean
            incr = alpha * diff
            self.mean += incr
            self.var = (1 - alpha) * (self.var + diff * incr)
