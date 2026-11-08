"""
Pythonic wrapper for citybrain/calibration.h: a bounded parameter update that
cannot leave [lo, hi] and cannot move by more than max_step in one call.
"""
from __future__ import annotations

from dataclasses import dataclass

from citybrain._raw import ffi, lib
from citybrain.errors import check


@dataclass(frozen=True)
class CalibrationBounds:
    lo: float = 0.0
    hi: float = 1.0
    max_step: float = 0.05
    rate: float = 0.1


@dataclass(frozen=True)
class CalibrationResult:
    value: float
    raw_step: float
    applied_step: float
    step_clipped: bool
    bound_clipped: bool


def default_bounds() -> CalibrationBounds:
    b = ffi.new("cb_calibration_bounds_t *")
    check(lib.cb_calibration_bounds_init(b))
    return CalibrationBounds(lo=b.lo, hi=b.hi, max_step=b.max_step, rate=b.rate)


def _to_c(bounds: CalibrationBounds):
    b = ffi.new("cb_calibration_bounds_t *")
    b.lo = bounds.lo
    b.hi = bounds.hi
    b.max_step = bounds.max_step
    b.rate = bounds.rate
    return b


def step(bounds: CalibrationBounds, current: float, gradient: float) -> CalibrationResult:
    """One bounded update. `current` must already lie inside [lo, hi] --
    raises DomainError otherwise, since that means the caller's stored state
    has already escaped the contract."""
    out = ffi.new("cb_calibration_result_t *")
    check(lib.cb_calibration_step(_to_c(bounds), current, gradient, out))
    return CalibrationResult(
        value=out.value, raw_step=out.raw_step, applied_step=out.applied_step,
        step_clipped=bool(out.step_clipped), bound_clipped=bool(out.bound_clipped),
    )


def min_steps(bounds: CalibrationBounds, from_value: float, to_value: float) -> int:
    """Minimum number of accepted updates needed to move from `from_value`
    to `to_value`, ignoring the gradient: ceil(|to-from| / max_step)."""
    out = ffi.new("int32_t *")
    check(lib.cb_calibration_min_steps(_to_c(bounds), from_value, to_value, out))
    return out[0]
