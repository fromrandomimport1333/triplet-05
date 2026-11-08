"""Rule fallback: standard actions per incident type (team plan 04, section 4).

The rules are data (config/rules.yaml): action templates by target type, resolved to concrete
devices from the city description. The choice of a unit is delegated to `pick_unit`, given by
the scoring service, so the rules and the scoring use the same unit allocation (free units,
priority between incidents, pre-emption).
"""

from __future__ import annotations

from typing import Any, Callable

from ..common.models import SEVERITY_RANK, Incident, ProposalAction
from ..scoring.city import CityView


# pick_unit(unit_type, template params, taken unit ids) -> (action or None, note or None)
UnitPicker = Callable[[str, dict[str, Any], set[str]], tuple[ProposalAction | None, str | None]]


class RuleEngine:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.templates: dict[str, list[dict[str, Any]]] = cfg.get("incidents", {})

    def propose(self, inc: Incident, city: CityView, *, exclude: set[str],
                pick_unit: UnitPicker) -> tuple[list[ProposalAction], list[str]]:
        """Actions for one incident, and notes on what could not be resolved."""
        actions: list[ProposalAction] = []
        notes: list[str] = []
        taken: set[str] = set()
        unit_types = {u.type for u in city.units.values()}
        for t in self.templates.get(inc.type, []):
            need = t.get("min_severity")
            if need and SEVERITY_RANK[inc.severity] < SEVERITY_RANK[need]:
                continue
            params = self._render(t.get("params") or {}, inc)
            target = t["target"]
            if t.get("select") == "nearest_free" or target in unit_types:
                for _ in range(int(t.get("max", 1))):
                    action, note = pick_unit(target, params, taken)
                    if note:
                        notes.append(note)
                    if action is None:
                        break
                    taken.add(action.target_id)
                    actions.append(action)
                continue
            found = self._actuators(t, inc.zone_id, city, exclude)
            if not found:
                notes.append(f"no usable {target} for {inc.zone_id}")
            for aid in found[: int(t["max"])] if t.get("max") else found:
                actions.append(ProposalAction(target_id=aid, action=t["action"], params=dict(params)))
        return actions, notes

    def justification(self, inc: Incident, actions: list[ProposalAction], notes: list[str]) -> str:
        acts = ", ".join(f"{a.action} {a.target_id}" for a in actions) or "no action"
        text = f"standard response to {inc.type} at {inc.zone_id} ({inc.severity}): {acts}"
        if notes:
            text += "; " + "; ".join(notes)
        return text[: int(self.cfg.get("max_justification_chars", 300))]

    # --- helpers --------------------------------------------------------------------

    @staticmethod
    def _render(params: dict[str, Any], inc: Incident) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in params.items():
            out[k] = v.format(zone_id=inc.zone_id, incident_id=inc.incident_id) if isinstance(v, str) else v
        return out

    @staticmethod
    def _actuators(t: dict[str, Any], zone: str, city: CityView, exclude: set[str]) -> list[str]:
        how = t.get("select", "in_zone")
        out = []
        for a in city.actuators.values():
            if a.type != t["target"] or t["action"] not in a.actions:
                continue
            if a.actuator_id in city.quarantined or a.actuator_id in exclude:
                continue
            controls = a.controls or [a.zone_id]
            if how == "controls_zone":
                ok = zone in controls
            elif how == "in_zone":
                ok = a.zone_id == zone or zone in controls
            else:
                raise ValueError(f"unknown select {how!r} in rules.yaml")
            if ok:
                out.append(a.actuator_id)
        return sorted(out)
