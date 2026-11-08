"""City registry learned from twin/meta + unit aliases/conversion + per-sensor limits.

Per-sensor unit/min/max come from twin/meta and replace the built-in envelopes. Rate, step
and noise thresholds come from SENSOR_LIMITS_FILE (per sensor type, in the unit written in
the file) and are converted into the sensor's own meta unit. Nothing city-specific is in code.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..detection.plausibility import PHYSICAL_LIMITS, SensorLimits

# canonical unit -> (dimension, factor to the dimension's base unit). Linear units only.
_UNITS: dict[str, tuple[str, float]] = {
    "m": ("length", 1.0), "cm": ("length", 0.01), "mm": ("length", 0.001),
    "veh/h": ("veh_rate", 1.0), "veh/min": ("veh_rate", 60.0), "veh/s": ("veh_rate", 3600.0),
    "veh/km": ("veh_density", 1.0),
    "km/h": ("speed", 1.0), "m/s": ("speed", 3.6),
    "kw": ("power", 1.0), "w": ("power", 0.001), "mw": ("power", 1000.0),
    "ug/m3": ("concentration", 1.0), "mg/m3": ("concentration", 1000.0),
    "mm/h": ("rain", 1.0), "c": ("temperature", 1.0),
}
_ALIASES = {
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "vehicles/h": "veh/h", "vehicles/hour": "veh/h", "vph": "veh/h", "veh/hr": "veh/h",
    "vehicles/min": "veh/min", "vehicles/minute": "veh/min", "vpm": "veh/min",
    "vehicles/s": "veh/s", "vehicles/km": "veh/km",
    "kmh": "km/h", "kph": "km/h", "km/hr": "km/h",
    "µg/m3": "ug/m3", "µg/m³": "ug/m3", "ug/m³": "ug/m3", "ug/m^3": "ug/m3", "μg/m3": "ug/m3",
    "μg/m³": "ug/m3", "°c": "c", "degc": "c",
}


def norm_unit(u: str | None) -> str:
    u = (u or "").strip().lower().replace(" ", "")
    return _ALIASES.get(u, u)


def unit_factor(src: str, dst: str) -> float | None:
    """Multiplier converting a value in `src` into `dst`; None if not convertible."""
    a, b = norm_unit(src), norm_unit(dst)
    if a == b:
        return 1.0
    if a not in _UNITS or b not in _UNITS or _UNITS[a][0] != _UNITS[b][0]:
        return None
    return _UNITS[a][1] / _UNITS[b][1]


@dataclass(frozen=True)
class TypeRule:
    """Thresholds for one sensor type, expressed in `unit`."""
    unit: str
    max_rate_per_s: float = math.inf
    max_step: float = math.inf
    noise_floor: float = 0.0
    min_value: float | None = None   # used only when the sensor is not in twin/meta
    max_value: float | None = None


def load_type_rules(path: str = "") -> dict[str, TypeRule]:
    """Rules from SENSOR_LIMITS_FILE (JSON {type: {unit, max_rate_per_s, max_step, noise_floor,
    min, max}}). Without a file: the native PHYSICAL_LIMITS (rate only)."""
    rules = {t: TypeRule(l.unit, l.max_rate_per_s, l.max_step, 0.0, l.min_value, l.max_value)
             for t, l in PHYSICAL_LIMITS.items()}
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for t, r in raw.items():
            if t.startswith("_"):
                continue  # comments
            inf = lambda k: math.inf if r.get(k) is None else float(r[k])  # noqa: E731
            rules[t] = TypeRule(r["unit"], inf("max_rate_per_s"), inf("max_step"),
                                float(r.get("noise_floor") or 0.0), r.get("min"), r.get("max"))
    return rules


@dataclass
class CityRegistry:
    type_rules: dict[str, TypeRule] = field(default_factory=dict)
    meta_id: str | None = None
    zones: set[str] = field(default_factory=set)
    sensors: dict[str, dict] = field(default_factory=dict)
    actuators: dict[str, dict] = field(default_factory=dict)
    units: dict[str, dict] = field(default_factory=dict)

    # ---------- twin/meta ----------
    @staticmethod
    def validate_meta(body: dict) -> str | None:
        """Return an error string, or None if the body matches the kit's external-meta schema
        shape (required fields and id patterns)."""
        from .envelope import valid_id
        for k in ("id", "timestamp", "source", "zones", "sensors"):
            if k not in body:
                return f"missing {k}"
        if not isinstance(body["zones"], list) or not isinstance(body["sensors"], list):
            return "zones/sensors must be arrays"
        for z in body["zones"]:
            if not isinstance(z, dict) or not valid_id(z.get("zone_id")):
                return "bad zone"
        for s in body["sensors"]:
            if not isinstance(s, dict) or not valid_id(s.get("sensor_id")) or not valid_id(s.get("zone_id")):
                return "bad sensor id"
            for k in ("type", "unit", "min", "max"):
                if k not in s:
                    return f"sensor {s.get('sensor_id')} missing {k}"
            if isinstance(s["min"], bool) or not isinstance(s["min"], (int, float)) \
                    or isinstance(s["max"], bool) or not isinstance(s["max"], (int, float)):
                return f"sensor {s['sensor_id']} min/max not numbers"
        for a in body.get("actuators") or []:
            if not isinstance(a, dict) or not valid_id(a.get("actuator_id")):
                return "bad actuator"
        for u in body.get("units") or []:
            if not isinstance(u, dict) or not valid_id(u.get("unit_id")):
                return "bad unit"
        return None

    def load_meta(self, body: dict) -> bool:
        """Replace the registry with this city. Returns True if the meta id changed."""
        changed = body.get("id") != self.meta_id
        self.meta_id = body.get("id")
        self.zones = {z["zone_id"] for z in body["zones"]}
        self.sensors = {s["sensor_id"]: dict(s) for s in body["sensors"]}
        self.actuators = {a["actuator_id"]: dict(a) for a in body.get("actuators") or []}
        self.units = {u["unit_id"]: dict(u) for u in body.get("units") or []}
        return changed

    @property
    def loaded(self) -> bool:
        return self.meta_id is not None

    def knows_device(self, device_id: str) -> bool:
        return device_id in self.sensors or device_id in self.actuators or device_id in self.units

    # ---------- limits ----------
    def limits_for(self, sensor_id: str, sensor_type: str) -> SensorLimits | None:
        """Limits in the sensor's OWN meta unit. None => pipeline uses its built-in table."""
        s = self.sensors.get(sensor_id)
        rule = self.type_rules.get(sensor_type)
        if s is None:
            if rule is None or rule.min_value is None or rule.max_value is None:
                return None
            return SensorLimits(norm_unit(rule.unit), float(rule.min_value), float(rule.max_value),
                                rule.max_rate_per_s, rule.max_step)
        unit = norm_unit(s["unit"])
        rate = step = math.inf
        if rule is not None:
            f = unit_factor(rule.unit, unit)
            if f is not None:
                rate, step = rule.max_rate_per_s * f, rule.max_step * f
        # unknown type or unconvertible rule unit: meta min/max only
        return SensorLimits(unit, float(s["min"]), float(s["max"]), rate, step)

    def noise_floor(self, sensor_id: str) -> float:
        s = self.sensors.get(sensor_id)
        if s is None:
            return 0.0
        rule = self.type_rules.get(s["type"])
        if rule is None:
            return 0.0
        f = unit_factor(rule.unit, s["unit"])
        return rule.noise_floor * f if f is not None else 0.0

    def to_meta_unit(self, sensor_id: str, sensor_type: str, value: float,
                     unit: str) -> tuple[float, str] | None:
        """Convert a reading into its sensor's meta unit (or, for a sensor not in twin/meta, into
        the unit of the configured type rule). None => not convertible (unit mismatch)."""
        s = self.sensors.get(sensor_id)
        target = s["unit"] if s is not None else (
            self.type_rules[sensor_type].unit if sensor_type in self.type_rules else unit)
        f = unit_factor(unit, target)
        if f is None:
            return None
        return value * f, norm_unit(target)
