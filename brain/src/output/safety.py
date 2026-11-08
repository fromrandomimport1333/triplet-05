"""Safety rules S1-S6 (team plan 05, section 4), pure functions of a snapshot of the city state.

`SafetyRules.check()` returns None when the action may be sent, else a Refusal naming the rule.
Rules run in order S1..S6 and the first that fails wins, so the reason shown is stable.

Evidence counting (S3, S6):
- a source counts when its trust is above `evidence.min_trust` (rejected readings have trust 0);
- each sensor is one source; its type comes from the city description;
- all emergency calls of an incident (detection groups them per zone) count together as ONE
  source, so a flood of calls from one zone never replaces a second sensor (scenario B7);
- S6 (Guardian offline) asks for 2 different SENSOR TYPES: calls never count there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Device:
    device_id: str
    kind: str                       # actuator | unit
    type: str
    zone_id: str | None
    controls: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Source:
    """One piece of evidence of an incident, after Guardian's current verdict is applied."""

    kind: str                       # reading | call
    source_id: str
    trust: float
    sensor_type: str | None = None


@dataclass(slots=True)
class Refusal:
    rule: str
    detail: str


@dataclass(slots=True)
class CheckInput:
    target_id: str
    action: str
    device: Device | None           # None = not in the city description
    has_meta: bool
    quarantined: bool
    sources: list[Source]
    guardian_offline: bool
    last_sent: float | None         # last time this action (or any action, see s4_scope) was sent
    now: float


class SafetyRules:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.enabled = {k.split("_", 1)[0]: bool(v) for k, v in cfg.get("rules", {}).items()}
        ev = cfg.get("evidence", {})
        self.min_trust = float(ev.get("min_trust", 0.0))
        self.calls_one_source = bool(ev.get("calls_count_as_one_source", True))
        self.unit_actions = list(cfg.get("unit_actions", ["dispatch", "recall"]))
        self.min_sources = int(cfg.get("s3_min_sources", 2))
        self.cooldown_s = float(cfg.get("s4_cooldown_s", 60))
        self.min_types = int(cfg.get("s6_min_sensor_types", 2))
        self.risky = list(cfg.get("risky_actions", []))

    # --- helpers ------------------------------------------------------------------------

    def on(self, rule: str) -> bool:
        return self.enabled.get(rule, True)

    def is_risky(self, device: Device | None, action: str) -> bool:
        if device is None:
            return False
        for r in self.risky:
            if r.get("action") != action or r.get("kind", device.kind) != device.kind:
                continue
            if r.get("type") in (None, device.type):
                return True
        return False

    def allowed_actions(self, device: Device) -> list[str]:
        return self.unit_actions if device.kind == "unit" else device.actions

    def agreeing(self, sources: list[Source]) -> tuple[list[str], set[str]]:
        """(names of the distinct agreeing sources, distinct sensor types among them)."""
        names: list[str] = []
        types: set[str] = set()
        calls = False
        for s in sources:
            if s.trust <= self.min_trust:
                continue
            if s.kind == "call":
                calls = True
                continue
            if s.source_id not in names:
                names.append(s.source_id)
                if s.sensor_type:
                    types.add(s.sensor_type)
        if calls and self.calls_one_source:
            names.append("calls")
        return names, types

    # --- the rules ----------------------------------------------------------------------

    def check(self, c: CheckInput) -> Refusal | None:
        # unknown target first: nothing else can be judged without its description (S5)
        if self.on("S5") and c.device is None:
            why = "no city description received yet" if not c.has_meta else "not in the city description"
            return Refusal("S5", f"unknown target {c.target_id} ({why})")

        if self.on("S1") and c.quarantined:
            return Refusal("S1", f"{c.target_id} is quarantined by Guardian")

        if self.on("S2") and c.sources and all(s.trust <= 0 for s in c.sources):
            ids = ", ".join(s.source_id for s in c.sources[:4])
            return Refusal("S2", f"all evidence of the incident was rejected by Guardian ({ids})")

        risky = self.is_risky(c.device, c.action)
        names, types = self.agreeing(c.sources)
        if self.on("S3") and risky and len(names) < self.min_sources:
            have = ", ".join(names) if names else "none"
            return Refusal("S3", f"risky action needs {self.min_sources} different sources agreeing, "
                                 f"has {len(names)} ({have})")

        if self.on("S4") and c.last_sent is not None and c.now - c.last_sent < self.cooldown_s:
            return Refusal("S4", f"same action on {c.target_id} {c.now - c.last_sent:.0f} s ago "
                                 f"(cooldown {self.cooldown_s:g} s)")

        if self.on("S5") and c.device is not None and c.action not in self.allowed_actions(c.device):
            allowed = "/".join(self.allowed_actions(c.device)) or "none"
            return Refusal("S5", f"action {c.action!r} not allowed on {c.target_id} ({c.device.type}: {allowed})")

        if self.on("S6") and risky and c.guardian_offline and len(types) < self.min_types:
            have = ", ".join(sorted(types)) if types else "none"
            return Refusal("S6", f"Guardian offline: risky action needs {self.min_types} different sensor "
                                 f"types, has {len(types)} ({have})")
        return None
