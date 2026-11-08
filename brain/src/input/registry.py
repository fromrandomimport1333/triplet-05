"""City description (twin/meta) kept in memory for the known-ID and range checks."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..common.models import ActuatorDecl, MetaBody, SensorDecl, UnitDecl, ZoneDecl


@dataclass
class CityRegistry:
    zones: dict[str, ZoneDecl] = field(default_factory=dict)
    sensors: dict[str, SensorDecl] = field(default_factory=dict)
    actuators: dict[str, ActuatorDecl] = field(default_factory=dict)
    units: dict[str, UnitDecl] = field(default_factory=dict)
    meta_id: str | None = None

    @property
    def loaded(self) -> bool:
        return self.meta_id is not None

    def is_device(self, device_id: str) -> bool:
        return device_id in self.sensors or device_id in self.actuators or device_id in self.units

    @staticmethod
    def problems(meta: MetaBody) -> list[str]:
        """References in the city description that point nowhere, and duplicate ids (MSG-3)."""
        out: list[str] = []
        zones = [z.zone_id for z in meta.zones]
        known = set(zones)
        devices = (
            [s.sensor_id for s in meta.sensors]
            + [a.actuator_id for a in meta.actuators]
            + [u.unit_id for u in meta.units]
        )
        for name, ids in (("zone", zones), ("device", devices)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                out.append(f"duplicate {name} ids {dupes}")
        for z in meta.zones:
            out += [f"zone {z.zone_id} adjacent to unknown zone {a}" for a in z.adjacent if a not in known]
        for s in meta.sensors:
            if s.zone_id not in known:
                out.append(f"sensor {s.sensor_id} in unknown zone {s.zone_id}")
            if s.min > s.max:
                out.append(f"sensor {s.sensor_id} has min > max")
        for a in meta.actuators:
            out += [
                f"actuator {a.actuator_id} uses unknown zone {zid}"
                for zid in [a.zone_id, *a.controls]
                if zid not in known
            ]
        for u in meta.units:
            if u.home_zone and u.home_zone not in known:
                out.append(f"unit {u.unit_id} has unknown home zone {u.home_zone}")
        return out

    def load(self, meta: MetaBody) -> None:
        self.zones = {z.zone_id: z for z in meta.zones}
        self.sensors = {s.sensor_id: s for s in meta.sensors}
        self.actuators = {a.actuator_id: a for a in meta.actuators}
        self.units = {u.unit_id: u for u in meta.units}
        self.meta_id = meta.id
