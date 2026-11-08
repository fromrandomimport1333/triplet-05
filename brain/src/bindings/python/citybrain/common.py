"""
Pythonic wrapper for citybrain/common.h: library version information.
"""
from __future__ import annotations

from citybrain._raw import ffi, lib


def version_string() -> str:
    out = ffi.new("const char **")
    lib.cb_version_string(out)
    return ffi.string(out[0]).decode("ascii")


def version_numbers() -> "tuple[int, int, int]":
    major = ffi.new("int32_t *")
    minor = ffi.new("int32_t *")
    patch = ffi.new("int32_t *")
    lib.cb_version_numbers(major, minor, patch)
    return (major[0], minor[0], patch[0])
