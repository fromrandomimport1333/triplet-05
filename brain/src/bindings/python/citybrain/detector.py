"""
Wrapper for citybrain/detector.h: EWMA-baselined, two-sided CUSUM change-
point detection. Answers whether a stream shows a sustained shift, not
whether it can be trusted -- that's a different layer's job.
"""
from __future__ import annotations

from dataclasses import dataclass

from citybrain._raw import ffi, lib
from citybrain.errors import check

FLAG_WARMUP = 0x01
FLAG_SHIFT_UP = 0x02
FLAG_SHIFT_DOWN = 0x04
FLAG_BASELINE_HELD = 0x08


@dataclass(frozen=True)
class DetectorConfig:
    ewma_alpha: float = 0.10
    var_alpha: float = 0.05
    min_sigma: float = 1e-6
    cusum_slack: float = 0.5
    cusum_threshold: float = 5.0
    baseline_hold_enabled: bool = True
    baseline_hold_frac: float = 0.5
    warmup_samples: int = 20


@dataclass(frozen=True)
class DetectorOutput:
    flags: int
    baseline: float
    sigma: float
    residual: float
    normalised_residual: float
    cusum_high: float
    cusum_low: float

    @property
    def warmup(self) -> bool:
        return bool(self.flags & FLAG_WARMUP)

    @property
    def shift_up(self) -> bool:
        return bool(self.flags & FLAG_SHIFT_UP)

    @property
    def shift_down(self) -> bool:
        return bool(self.flags & FLAG_SHIFT_DOWN)

    @property
    def baseline_held(self) -> bool:
        return bool(self.flags & FLAG_BASELINE_HELD)


def default_config() -> DetectorConfig:
    c = ffi.new("cb_detector_config_t *")
    check(lib.cb_detector_config_init(c))
    return _config_from_c(c)


def _config_from_c(c) -> DetectorConfig:
    return DetectorConfig(
        ewma_alpha=c.ewma_alpha, var_alpha=c.var_alpha, min_sigma=c.min_sigma,
        cusum_slack=c.cusum_slack, cusum_threshold=c.cusum_threshold,
        baseline_hold_enabled=bool(c.baseline_hold_enabled),
        baseline_hold_frac=c.baseline_hold_frac,
        warmup_samples=c.warmup_samples,
    )


def _to_c_config(cfg: DetectorConfig):
    c = ffi.new("cb_detector_config_t *")
    c.ewma_alpha = cfg.ewma_alpha
    c.var_alpha = cfg.var_alpha
    c.min_sigma = cfg.min_sigma
    c.cusum_slack = cfg.cusum_slack
    c.cusum_threshold = cfg.cusum_threshold
    c.baseline_hold_enabled = 1 if cfg.baseline_hold_enabled else 0
    c.baseline_hold_frac = cfg.baseline_hold_frac
    c.warmup_samples = cfg.warmup_samples
    return c


class Detector:
    """Stateful change-point detector for one stream."""

    def __init__(self, config: DetectorConfig, first_value: float, initial_sigma: float):
        self._config = config
        self._state = ffi.new("cb_detector_state_t *")
        check(lib.cb_detector_init(self._state, _to_c_config(config), first_value, initial_sigma))

    def update(self, value: float) -> DetectorOutput:
        """Feed one measurement; returns the detector's output for it."""
        out = ffi.new("cb_detector_output_t *")
        check(lib.cb_detector_update(self._state, _to_c_config(self._config), value, out))
        return DetectorOutput(
            flags=out.flags, baseline=out.baseline, sigma=out.sigma,
            residual=out.residual, normalised_residual=out.normalised_residual,
            cusum_high=out.cusum_high, cusum_low=out.cusum_low,
        )

    def reset_alarms(self) -> None:
        """Clear both CUSUM accumulators, keeping the learned baseline."""
        check(lib.cb_detector_reset_alarms(self._state))

    def baseline(self) -> "tuple[float, float]":
        """(baseline, sigma) without feeding a sample."""
        b = ffi.new("double *")
        s = ffi.new("double *")
        check(lib.cb_detector_baseline(self._state, _to_c_config(self._config), b, s))
        return (b[0], s[0])


def arl0(cusum_slack: float, cusum_threshold: float) -> float:
    """Approximate average run length to false alarm (Siegmund's approximation)."""
    out = ffi.new("double *")
    check(lib.cb_detector_arl0(cusum_slack, cusum_threshold, out))
    return out[0]


def arl1(cusum_slack: float, cusum_threshold: float, shift_sigmas: float) -> float:
    """Approximate expected detection delay for a sustained shift of
    `shift_sigmas` standard deviations."""
    out = ffi.new("double *")
    check(lib.cb_detector_arl1(cusum_slack, cusum_threshold, shift_sigmas, out))
    return out[0]
