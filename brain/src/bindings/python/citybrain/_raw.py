"""
Access to the compiled cffi extension. Internal: other modules import ffi
and lib from here; use citybrain.fusion, citybrain.detector etc. instead
of this module directly.
"""
try:
    from citybrain._citybrain_cffi import ffi, lib
except ImportError as exc:  # pragma: no cover - environment-dependent
    raise ImportError(
        "citybrain's compiled extension (_citybrain_cffi) was not found. "
        "Build it with `pip install -e bindings/python` from the repository "
        "root, which runs build_citybrain.py and also builds the C core "
        "automatically if needed. See docs/build.md."
    ) from exc

__all__ = ["ffi", "lib"]
