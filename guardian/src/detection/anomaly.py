"""Pipeline steps 4-5 — per-device statistical baseline (EWMA mean/variance -> z-score),
stuck-sensor detection, and score -> verdict mapping."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

TRUSTED_THRESHOLD = 0.8
REJECT_THRESHOLD = 0.4
STUCK_SCORE = 0.6  # inside the 'suspicious' band on purpose: logged, never auto-blocked


def map_score_to_verdict(score: float) -> str:
    """Step 5: >=0.8 trusted, 0.4<=score<0.8 suspicious, <0.4 rejected."""
    if score >= TRUSTED_THRESHOLD:
        return "trusted"
    if score >= REJECT_THRESHOLD:
        return "suspicious"
    return "rejected"


@dataclass
class _Baseline:
    mean: float = 0.0
    var: float = 0.0
    n: int = 0
    last_value: float | None = None
    same_since: datetime | None = None
    same_count: int = 1   # consecutive readings with exactly last_value


class EwmaAnomalyDetector:
    """Rolling EWMA baseline per device.

    score = 1                      if z <= z_ok
          = 1 - (z - z_ok) / span  otherwise (clamped to [0, 1])
    With defaults (z_ok=2, span=8): z=3.6 -> 0.8, z=6.8 -> 0.4.
    A value identical for >= stuck_window_s, or (if stuck_count > 0) for >= stuck_count
    consecutive readings, is capped at STUCK_SCORE (suspicious).
    """

    def __init__(self, alpha: float = 0.1, warmup: int = 10, z_ok: float = 2.0,
                 span: float = 8.0, stuck_window_s: float = 300.0, min_std_ratio: float = 0.01,
                 stuck_count: int = 0) -> None:
        """Configure smoothing `alpha`, `warmup` samples, z mapping and stuck window."""
        self.alpha, self.warmup, self.z_ok, self.span = alpha, warmup, z_ok, span
        self.stuck_window_s = stuck_window_s
        self.stuck_count = stuck_count   # 0 = count rule off (native default)
        self.min_std_ratio = min_std_ratio
        self._b: dict[str, _Baseline] = {}
        # optional: device_id -> absolute std floor (Brain profile: per-type noise_floor from config)
        self.std_floor_provider = None

    def zscore(self, device_id: str, value: float) -> float:
        """Current z-score of `value` against the device baseline (0 during warm-up)."""
        b = self._b.get(device_id)
        if b is None or b.n < self.warmup:
            return 0.0
        std = max(math.sqrt(b.var), self.min_std_ratio * max(1.0, abs(b.mean)))
        if self.std_floor_provider is not None:
            std = max(std, float(self.std_floor_provider(device_id) or 0.0))
        return abs(value - b.mean) / std

    def score(self, device_id: str, value: float, ts: datetime) -> tuple[float, str]:
        """Return (trust_score in [0,1], reason). Pure: does not update the baseline."""
        b = self._b.get(device_id)
        if b is not None and b.last_value is not None and value == b.last_value and b.same_since:
            stuck_for = (ts - b.same_since).total_seconds()
            if stuck_for >= self.stuck_window_s:
                return STUCK_SCORE, f"stuck_sensor: identical value {value} for {stuck_for:.0f}s"
            if self.stuck_count and b.same_count + 1 >= self.stuck_count:
                return STUCK_SCORE, (f"stuck_sensor: identical value {value} for "
                                     f"{b.same_count + 1} consecutive readings")
        if b is None or b.n < self.warmup:
            return 1.0, f"anomaly: warm-up ({0 if b is None else b.n}/{self.warmup} samples)"
        z = self.zscore(device_id, value)
        s = 1.0 if z <= self.z_ok else max(0.0, 1.0 - (z - self.z_ok) / self.span)
        return round(s, 4), f"anomaly: z={z:.2f} vs EWMA mean={b.mean:.3f}"

    def update(self, device_id: str, value: float, ts: datetime) -> None:
        """Fold `value` into the baseline (call only for non-rejected readings)."""
        b = self._b.setdefault(device_id, _Baseline())
        if b.n == 0:
            b.mean, b.var = value, 0.0
        else:
            d = value - b.mean
            b.mean += self.alpha * d
            b.var = (1 - self.alpha) * (b.var + self.alpha * d * d)
        b.n += 1
        if b.last_value is None or value != b.last_value:
            b.last_value, b.same_since, b.same_count = value, ts, 1
        else:
            b.same_count += 1
