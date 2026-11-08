"""The mock city: zones, sensors, actuators and response units, read from `mocks/city.yaml`.

Plain data, no simulation. `core.py` builds its state from a `City`; `Twin.meta()` describes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CITY_FILE = Path(__file__).resolve().parents[1] / "city.yaml"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
UNIT_ACTIONS = ["dispatch", "recall"]


@dataclass
class City:
    zones: list[dict[str, Any]] = field(default_factory=list)
    sensors: dict[str, dict[str, Any]] = field(default_factory=dict)      # sensor_id -> zone_id, type, unit, min, max, alert, baseline, noise_abs
    actuators: dict[str, dict[str, Any]] = field(default_factory=dict)    # actuator_id -> type, zone_id, controls, actions
    units: dict[str, dict[str, Any]] = field(default_factory=dict)        # unit_id -> type, home_zone

    @property
    def zone_ids(self) -> list[str]:
        return [z["zone_id"] for z in self.zones]


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"city: bad {what} {value!r} (lower case letters, digits and - only)")
    return value


def parse_city(raw: dict[str, Any]) -> City:
    """Validate the yaml content and expand the per-zone sensors."""
    types = raw.get("sensor_types") or {}
    zones = [dict(z) for z in raw.get("zones") or []]
    if not zones:
        raise ValueError("city: at least one zone is needed")
    city = City()
    known = [_check_id(z.get("zone_id"), "zone_id") for z in zones]
    if len(set(known)) != len(known):
        raise ValueError("city: duplicate zone_id")
    for z in zones:
        z.setdefault("adjacent", [])
        for a in z["adjacent"]:
            if a not in known:
                raise ValueError(f"city: zone {z['zone_id']} is adjacent to unknown zone {a!r}")
        wanted = z.pop("sensors", None) or [t for t, sp in types.items()
                                            if sp.get("zones") is None or z["zone_id"] in sp["zones"]]
        number = re.search(r"(\d+)$", z["zone_id"])
        for t in wanted:
            if t not in types:
                raise ValueError(f"city: zone {z['zone_id']} uses unknown sensor type {t!r}")
            spec = types[t]
            sid = _check_id(f"{spec['prefix']}-{number.group(1) if number else z['zone_id']}", "sensor_id")
            city.sensors[sid] = {"zone_id": z["zone_id"], "type": t, "unit": spec["unit"], "min": spec["min"],
                                 "max": spec["max"], "alert": spec.get("alert"), "baseline": float(spec["baseline"]),
                                 "noise_abs": float(spec.get("noise_abs", 0))}
    city.zones = zones
    for a in raw.get("actuators") or []:
        aid = _check_id(a.get("actuator_id"), "actuator_id")
        zones_used = [a["zone_id"], *a.get("controls", [a["zone_id"]])]
        if any(z not in known for z in zones_used) or aid in city.actuators:
            raise ValueError(f"city: actuator {aid} has an unknown zone or a duplicate id")
        city.actuators[aid] = {"type": a["type"], "zone_id": a["zone_id"],
                               "controls": list(a.get("controls", [a["zone_id"]])), "actions": list(a["actions"])}
    for u in raw.get("units") or []:
        uid = _check_id(u.get("unit_id"), "unit_id")
        if u.get("home_zone") not in known or uid in city.units or uid in city.actuators:
            raise ValueError(f"city: unit {uid} has an unknown home zone or a duplicate id")
        city.units[uid] = {"type": u["type"], "home_zone": u["home_zone"]}
    return city


def load_city(path: str | Path | None = None) -> City:
    with open(path or DEFAULT_CITY_FILE, encoding="utf-8") as f:
        return parse_city(yaml.safe_load(f) or {})
