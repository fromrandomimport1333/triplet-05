"""Load the YAML files of brain/config/. Paths in them are relative to brain/.

BRAIN_CONFIG_DIR replaces the config folder. BRAIN_CONFIG_OVERLAY names a folder of partial
YAML files merged on top (e.g. config/overlays/docker: only the broker host and service
addresses differ), so a deployment changes a few values without copying every file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BRAIN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("BRAIN_CONFIG_DIR", BRAIN_ROOT / "config"))


def _overlay_dir() -> Path | None:
    value = os.environ.get("BRAIN_CONFIG_OVERLAY")
    return resolve(value) if value else None


def merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Deep merge: dicts are merged key by key, any other value of `top` replaces."""
    out = dict(base)
    for key, value in top.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


@lru_cache(maxsize=None)
def load(name: str) -> dict[str, Any]:
    """Return the content of config/<name>.yaml as a dict (with the overlay, if any)."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    overlay = _overlay_dir()
    if overlay is not None and (overlay / f"{name}.yaml").is_file():
        with open(overlay / f"{name}.yaml", encoding="utf-8") as f:
            data = merge(data, yaml.safe_load(f) or {})
    return data


def resolve(path: str | os.PathLike[str]) -> Path:
    """Make a config path absolute (relative paths start at brain/)."""
    p = Path(path)
    return p if p.is_absolute() else BRAIN_ROOT / p


def read_secret(path: str | None) -> str | None:
    """Read a secret file as stripped text; None if the path is empty or missing."""
    if not path:
        return None
    p = resolve(path)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip()


def outgoing_seal_keys(security: dict[str, Any] | None = None) -> dict[str, bytes] | None:
    """HMAC keys that seal what the Brain publishes outside brain/internal/ (brain/event, brain/cmd,
    brain/alert): one per receiving layer (security.yaml outgoing.hmacs_for). None if sealing is off.
    Exits with a clear message when a key file is missing (entry points only)."""
    security = security or load("security")
    out = security.get("outgoing", {})
    if not out.get("seal", True):
        return None
    keys: dict[str, bytes] = {}
    for layer in out.get("hmacs_for", []):
        text = read_secret(security["hmac_keys"][layer])
        if not text:
            raise SystemExit(f"missing HMAC key for {layer}: {resolve(security['hmac_keys'][layer])} "
                             "(run python tools/setup.py, see brain/README.md)")
        keys[layer] = text.encode("utf-8")
    return keys
