"""What the scoring and the rule fallback know about the city.

Built only from internal topics: brain/internal/meta (zones, adjacency, devices, units),
quarantine/+, trust/+, ack (unit status and position) and layer-status/guardian.
No id is hard-coded: everything comes from the city description.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..common.models import ActuatorDecl, InternalMeta, SensorDecl, UnitDecl, ZoneDecl

BUSY = ("en_route", "on_scene", "returning")


class CityView:
    def __init__(self, unit_actions: list[str], verdict_values: dict[str, float]):
        self.unit_actions = list(unit_actions)
        self.verdict_values = verdict_values
        self.zones: dict[str, ZoneDecl] = {}
        self.adjacency: dict[str, set[str]] = {}
        self.sensors: dict[str, SensorDecl] = {}
        self.actuators: dict[str, ActuatorDecl] = {}
        self.units: dict[str, UnitDecl] = {}
        self.quarantined: set[str] = set()
        self.unit_status: dict[str, str] = {}
        self.unit_zone: dict[str, str] = {}
        self.trust: dict[str, float] = {}           # sensor_id -> Guardian's latest trust 0..1
        self.guardian_status: str | None = None
        self._hops: dict[str, dict[str, int]] = {}

    @property
    def has_meta(self) -> bool:
        return bool(self.zones)

    # --- updates ------------------------------------------------------------------

    def apply_meta(self, msg: dict[str, Any]) -> None:
        meta = InternalMeta.model_validate(msg)
        self.zones = {z.zone_id: z for z in meta.zones}
        adj: dict[str, set[str]] = {z: set() for z in self.zones}
        for z in meta.zones:                        # adjacency is used both ways
            for n in z.adjacent:
                adj.setdefault(z.zone_id, set()).add(n)
                adj.setdefault(n, set()).add(z.zone_id)
        self.adjacency = adj
        self._hops = {}
        self.sensors = {s.sensor_id: s for s in meta.sensors}
        self.actuators = {a.actuator_id: a for a in meta.actuators}
        self.units = {u.unit_id: u for u in meta.units}

    def apply_quarantine(self, msg: dict[str, Any]) -> None:
        device, state = msg.get("device_id"), msg.get("state")
        if not device:
            return
        if state == "quarantined":
            self.quarantined.add(device)
        elif state == "released":
            self.quarantined.discard(device)

    def apply_trust(self, msg: dict[str, Any]) -> None:
        sensor = msg.get("sensor_id")
        if not sensor:
            return
        score = msg.get("trust_score")
        if isinstance(score, (int, float)):
            self.trust[sensor] = max(0.0, min(1.0, float(score)))
        elif msg.get("verdict") in self.verdict_values:
            self.trust[sensor] = float(self.verdict_values[msg["verdict"]])

    def apply_ack(self, msg: dict[str, Any]) -> tuple[str, str | None, str] | None:
        """Unit status update. Returns (unit_id, old status, new status) when the status changes."""
        if msg.get("kind") != "unit":
            return None
        uid = msg.get("target_id")
        if not uid:
            return None
        if msg.get("zone_id"):
            self.unit_zone[uid] = msg["zone_id"]
        status = msg.get("status")
        if not status:
            return None
        old = self.unit_status.get(uid)
        self.unit_status[uid] = status
        return (uid, old, status) if old != status else None

    # --- queries ------------------------------------------------------------------

    def hops(self, a: str | None, b: str | None) -> int | None:
        """Zone hops between two zones (BFS on adjacency), None if unknown or unreachable."""
        if a is None or b is None or a not in self.adjacency:
            return None
        if a not in self._hops:
            dist = {a: 0}
            q = deque([a])
            while q:
                z = q.popleft()
                for n in self.adjacency.get(z, ()):
                    if n not in dist:
                        dist[n] = dist[z] + 1
                        q.append(n)
            self._hops[a] = dist
        return self._hops[a].get(b)

    def is_unit(self, target_id: str) -> bool:
        return target_id in self.units

    def target_type(self, target_id: str) -> str | None:
        if target_id in self.actuators:
            return self.actuators[target_id].type
        if target_id in self.units:
            return self.units[target_id].type
        return None

    def allowed(self, target_id: str, action: str) -> bool:
        if target_id in self.actuators:
            return action in self.actuators[target_id].actions
        if target_id in self.units:
            return action in self.unit_actions
        return False

    def unit_position(self, uid: str) -> str | None:
        return self.unit_zone.get(uid) or self.units[uid].home_zone

    def unit_free(self, uid: str) -> bool:
        return self.unit_status.get(uid, "free") not in BUSY

    def target_hops(self, target_id: str, zone_id: str) -> int | None:
        """Hops from a unit's position, or from the nearest zone an actuator acts on."""
        if target_id in self.units:
            return self.hops(self.unit_position(target_id), zone_id)
        act = self.actuators.get(target_id)
        if act is None:
            return None
        zones = act.controls or [act.zone_id]
        found = [h for h in (self.hops(z, zone_id) for z in zones) if h is not None]
        return min(found) if found else None

    def sensor_type(self, sensor_id: str) -> str | None:
        s = self.sensors.get(sensor_id)
        return s.type if s else None
