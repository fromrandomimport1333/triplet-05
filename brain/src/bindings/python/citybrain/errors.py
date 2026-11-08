"""
Exception hierarchy mapping the C core's cb_status_t codes onto ordinary
Python exceptions, so callers never see a raw integer status code.
"""
from __future__ import annotations

from citybrain._raw import ffi, lib


class CityBrainError(Exception):
    """Base class for every error this library raises."""


class NullArgumentError(CityBrainError):
    """A required argument was missing (C: CB_ERR_NULL)."""


class DomainError(CityBrainError):
    """An argument was outside its documented domain (C: CB_ERR_DOMAIN)."""


class NotFiniteError(CityBrainError):
    """An argument was NaN or infinite (C: CB_ERR_NOT_FINITE)."""


class CapacityError(CityBrainError):
    """A count or buffer size was outside the supported range
    (C: CB_ERR_CAPACITY)."""


class StateError(CityBrainError):
    """An object was not initialised, or became internally inconsistent
    (C: CB_ERR_STATE)."""


class NotFoundError(CityBrainError):
    """The requested element does not exist (C: CB_ERR_NOT_FOUND)."""


class NoSolutionError(CityBrainError):
    """The problem has no feasible solution (C: CB_ERR_NO_SOLUTION)."""


class NoConvergenceError(CityBrainError):
    """An iterative solver hit its iteration limit before converging
    (C: CB_ERR_NO_CONVERGE)."""


class UnsupportedError(CityBrainError):
    """A valid request this build does not implement (C: CB_ERR_UNSUPPORTED)."""


_STATUS_TO_EXCEPTION = {
    1: NullArgumentError,
    2: DomainError,
    3: NotFiniteError,
    4: CapacityError,
    5: StateError,
    6: NotFoundError,
    7: NoSolutionError,
    8: NoConvergenceError,
    9: UnsupportedError,
}


def _status_message(status: int) -> str:
    out = ffi.new("const char **")
    rc = lib.cb_status_message(status, out)
    if rc != 0 or out[0] == ffi.NULL:
        return f"unknown status code {status}"
    return ffi.string(out[0]).decode("utf-8", errors="replace")


def check(status: int) -> None:
    """Raise the matching exception for a non-zero status code."""
    if status == 0:
        return
    exc_cls = _STATUS_TO_EXCEPTION.get(int(status), CityBrainError)
    raise exc_cls(_status_message(status))
