"""The scoring formulas (team plan 04, section 6). Pure functions, every number from scoring.yaml.

    P      = confidence x severity_weight x zone_importance x (1 + waiting_s / waiting_scale_s)
    action = P x effectiveness - cost - risk_penalty - safe_penalty
             risk_penalty = risk_weight x risk x (1 - trust)
             safe_penalty = penalty x risk x P   (safe mode, risky action, not enough sensor types agree)
    proposal score = sum of the action scores that are > 0 (the others are dropped)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..common.models import ZoneDecl


@dataclass(slots=True)
class ActionScore:
    target_id: str
    action: str
    target_type: str
    effectiveness: float
    hops: int | None
    cost: float
    risk: float
    risk_penalty: float
    safe_penalty: float
    score: float

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


@dataclass(slots=True)
class ProposalScore:
    proposal_id: str
    origin: str
    priority: float
    trust: float
    mode: str
    actions: list[Any] = field(default_factory=list)          # kept ProposalAction
    breakdown: list[ActionScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)            # dropped / substituted, for the explanation
    missing: set[str] = field(default_factory=set)            # unit types wanted but not available
    score: float = 0.0


class ScoringModel:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.p_cfg = cfg["priority"]
        self.cost_cfg = cfg["cost"]
        self.risk_cfg = cfg["risk"]
        self.safe_cfg = cfg["safe_mode"]

    # --- priority ---------------------------------------------------------------------

    def zone_importance(self, zone: ZoneDecl | None) -> float:
        zi = self.p_cfg["zone_importance"]
        values = [float(zi.get("default", 1.0))]
        if zone is not None:
            if zone.type in zi.get("by_type", {}):
                values.append(float(zi["by_type"][zone.type]))
            if zone.exposure in zi.get("by_exposure", {}):
                values.append(float(zi["by_exposure"][zone.exposure]))
        return max(values)

    def severity_weight(self, severity: str) -> float:
        return float(self.p_cfg["severity_weight"][severity])

    def priority(self, confidence: float, severity: str, importance: float, waiting_s: float) -> float:
        scale = float(self.p_cfg["waiting_scale_s"])
        return (max(0.0, min(1.0, confidence)) * self.severity_weight(severity) * importance
                * (1.0 + max(0.0, waiting_s) / scale))

    # --- terms ----------------------------------------------------------------------

    def combine_trust(self, values: list[float]) -> float:
        if not values:
            return 0.0
        how = self.cfg["trust"].get("combine", "mean")
        if how == "min":
            return min(values)
        if how == "max":
            return max(values)
        return sum(values) / len(values)

    def effectiveness(self, incident_type: str, target_type: str, action: str) -> float:
        eff = self.cfg["effectiveness"]
        return float(eff.get(incident_type, {}).get(target_type, {}).get(action, eff.get("default", 0.0)))

    def risk(self, target_kind: str, action: str) -> float:
        return float(self.risk_cfg["by_target"].get(target_kind, {}).get(action, self.risk_cfg["default"]))

    def cost(self, hops: int | None, is_unit: bool) -> float:
        c = self.cost_cfg
        max_hops = float(c["max_hops"])
        h = max_hops if hops is None else min(float(hops), max_hops)
        base = float(c["unit_weight"] if is_unit else c["actuator_weight"])
        return max(0.0, min(1.0, float(c["distance_weight"]) * h / max_hops + base))

    def risk_penalty(self, risk: float, trust: float) -> float:
        return float(self.risk_cfg.get("weight", 1.0)) * risk * (1.0 - max(0.0, min(1.0, trust)))

    def safe_penalty(self, risk: float, safe: bool, agree: bool, priority: float = 1.0) -> float:
        """Scales with P, so a risky action loses in safe mode whatever the priority:
        it keeps a positive score only if effectiveness > penalty x risk."""
        if not safe or agree or risk < float(self.safe_cfg["risky_from"]):
            return 0.0
        return float(self.safe_cfg["penalty"]) * risk * priority

    def risk_level(self, risk: float) -> str:
        levels = self.risk_cfg["levels"]
        if risk >= float(levels["high"]):
            return "high"
        if risk >= float(levels["medium"]):
            return "medium"
        return "low"

    # --- one action -------------------------------------------------------------------

    def score_action(self, *, priority: float, incident_type: str, target_id: str, action: str,
                     target_type: str, is_unit: bool, hops: int | None, trust: float,
                     safe: bool, agree: bool, effect_action: str | None = None) -> ActionScore:
        """effect_action: the action whose effectiveness counts (a pre-emption `recall` is scored
        as the `dispatch` it prepares); risk and cost are those of `action`."""
        eff = self.effectiveness(incident_type, target_type, effect_action or action)
        cost = self.cost(hops, is_unit)
        risk = self.risk("unit" if is_unit else target_type, action)
        rp = self.risk_penalty(risk, trust)
        sp = self.safe_penalty(risk, safe, agree, priority)
        return ActionScore(target_id, action, target_type, eff, hops, cost, risk, rp, sp,
                           priority * eff - cost - rp - sp)
