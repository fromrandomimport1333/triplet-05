"""Pipeline step 3 — cross-sensor / physical sanity checks.

A reading is implausible if it is outside the physical range for its sensor type, if it
changes faster than physically possible relative to the last ACCEPTED reading of the same
device, if its timestamp is in the future, or if its unit does not match the sensor type.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SensorLimits:
    unit: str
    min_value: float
    max_value: float
    max_rate_per_s: float  # max |dvalue/dt|
    max_step: float = math.inf  # max |dvalue| vs the last accepted reading (per tick), see step_window_s


# Physical envelopes for the TSYP14 city sensors (agreed in INTEGRATION.md).
PHYSICAL_LIMITS: dict[str, SensorLimits] = {
    "water_level": SensorLimits("m", 0.0, 10.0, 0.05),
    "rainfall": SensorLimits("mm/h", 0.0, 300.0, 5.0),
    "temperature": SensorLimits("C", -30.0, 60.0, 0.5),
    "air_quality_pm25": SensorLimits("ug/m3", 0.0, 1000.0, 20.0),
    "traffic_flow": SensorLimits("veh/h", 0.0, 10_000.0, 100.0),
    "power_load": SensorLimits("kW", 0.0, 5_000.0, 100.0),
}

MAX_CLOCK_SKEW = timedelta(seconds=30)


class PlausibilityChecker:
    """Stateful plausibility checker (remembers the last accepted reading per device)."""

    def __init__(self, limits: dict[str, SensorLimits] | None = None, step_window_s: float = 30.0) -> None:
        """Use `limits` (defaults to PHYSICAL_LIMITS). The per-tick step rule (max_step) only
        applies while the last accepted reading is younger than `step_window_s`."""
        self.limits = limits or PHYSICAL_LIMITS
        self.step_window_s = step_window_s
        self._last: dict[str, tuple[float, datetime]] = {}

    def check(self, device_id: str, sensor_type: str, unit: str, value: float,
              ts: datetime, now: datetime | None = None,
              limits: SensorLimits | None = None) -> tuple[bool, str]:
        """Return (ok, reason). Does not mutate state; call `accept` once the verdict is final.
        `limits` overrides the per-type table (Brain profile: per-sensor limits from twin/meta)."""
        if not math.isfinite(value):
            return False, "plausibility: non-finite value"
        if now is not None and ts - now > MAX_CLOCK_SKEW:
            return False, "plausibility: timestamp in the future"
        lim = limits or self.limits.get(sensor_type)
        if lim is None:
            return True, "plausibility: unknown sensor_type, range check skipped"
        if unit != lim.unit:
            return False, f"plausibility: unit '{unit}' invalid for {sensor_type} (expected '{lim.unit}')"
        if not (lim.min_value <= value <= lim.max_value):
            return False, (f"plausibility: {sensor_type}={value}{unit} outside physical range "
                           f"[{lim.min_value},{lim.max_value}]")
        last = self._last.get(device_id)
        if last is not None:
            last_v, last_ts = last
            age = (ts - last_ts).total_seconds()
            if abs(value - last_v) > lim.max_step and age <= self.step_window_s:
                return False, (f"plausibility: {sensor_type} jumped {last_v}->{value}{unit} "
                               f"(step {abs(value - last_v):.3f} > max {lim.max_step} per tick)")
            dt = max(age, 1.0)
            rate = abs(value - last_v) / dt
            if rate > lim.max_rate_per_s:
                return False, (f"plausibility: {sensor_type} jumped {last_v}->{value}{unit} in {dt:.0f}s "
                               f"(rate {rate:.3f}/s > max {lim.max_rate_per_s}/s)")
        return True, "ok"

    def accept(self, device_id: str, value: float, ts: datetime) -> None:
        """Record `value` as the device's last accepted reading (only for non-rejected verdicts)."""
        self._last[device_id] = (value, ts)
