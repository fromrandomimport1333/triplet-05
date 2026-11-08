"""Message models (pydantic v2).

- External bodies: what the Twin and Guardian send (interface spec, sections 5-6).
  Unknown extra fields are ignored (MSG-5).
- Internal messages published by the input module on brain/internal/ (overview, section 3),
  plus brain/error, brain/status and brain/event bodies.

Each team adds the models of the messages it publishes. JSON Schemas are generated
from these models into contracts/schemas/ (python -m src.common.schemas).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

# MSG-2: lowercase letters, digits and dashes
EntityId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)]
MessageId = Annotated[str, Field(min_length=1, max_length=128)]
Number = Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]


class _External(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: MessageId
    timestamp: str
    source: Literal["twin", "guardian", "brain"]
    correlation_id: MessageId | None = None
    seq: StrictInt | None = None


# --- Twin -------------------------------------------------------------------------


class ZoneDecl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    zone_id: EntityId
    name: str | None = None
    type: str | None = None
    exposure: str | None = None
    adjacent: list[EntityId] = []


class SensorDecl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sensor_id: EntityId
    zone_id: EntityId
    type: str
    unit: str
    min: Number
    max: Number
    alert: Number | None = None


class ActuatorDecl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    actuator_id: EntityId
    type: str
    zone_id: EntityId
    controls: list[EntityId] = []
    actions: list[str]


class UnitDecl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    unit_id: EntityId
    type: str
    home_zone: EntityId | None = None


class MetaBody(_External):
    """twin/meta: city description (TW-1, TW-2)."""

    zones: list[ZoneDecl]
    sensors: list[SensorDecl]
    actuators: list[ActuatorDecl] = []
    units: list[UnitDecl] = []


class ReadingBody(_External):
    """city/{zone}/sensor/{type}/{sensor_id} (TW-4, TW-5)."""

    sensor_id: EntityId
    zone_id: EntityId
    type: str
    value: Number
    unit: str
    origin: Literal["real", "simulated"] | None = None


class CallBody(_External):
    """city/{zone}/call (TW-9, TW-10)."""

    call_id: EntityId
    zone_id: EntityId
    category: Literal["fire", "accident", "flood", "medical", "other"]
    text: Annotated[str, Field(max_length=280)] | None = None


class ActuatorStateBody(_External):
    """twin/actuator/{id}: acknowledgement of a command, or a state change (TW-13, TW-14)."""

    actuator_id: EntityId
    result: Literal["done", "rejected", "failed"] | None = None
    state: str | None = None
    reason: str | None = None


class UnitStateBody(_External):
    """twin/unit/{id}: acknowledgement, or unit status and zone (TW-13, TW-15)."""

    unit_id: EntityId
    result: Literal["done", "rejected", "failed"] | None = None
    status: Literal["free", "en_route", "on_scene", "returning"] | None = None
    zone_id: EntityId | None = None
    state: str | None = None
    reason: str | None = None


class StatusBody(_External):
    """{layer}/status (TR-6)."""

    status: Literal["online", "offline", "degraded"]


# --- Guardian ---------------------------------------------------------------------


class VerdictBody(_External):
    """guardian/verdict/{sensor_id} (GD-1 to GD-6). `suspect` is mapped to `suspicious` before."""

    sensor_id: EntityId
    verdict: Literal["trusted", "suspicious", "rejected"]
    reason: str
    trust_score: Annotated[Number, Field(ge=0, le=1)] | None = None
    details: str | None = None


class QuarantineBody(_External):
    """guardian/quarantine/{device_id} (GD-7)."""

    device_id: EntityId
    state: Literal["quarantined", "released"]
    reason: str


EXTERNAL_MODELS: dict[str, type[_External]] = {
    "meta": MetaBody,
    "reading": ReadingBody,
    "call": CallBody,
    "actuator": ActuatorStateBody,
    "unit": UnitStateBody,
    "verdict": VerdictBody,
    "quarantine": QuarantineBody,
    "twin_status": StatusBody,
    "guardian_status": StatusBody,
}


# --- Internal (published by the input module) --------------------------------------


class _Internal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    timestamp: str

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class InternalMeta(_Internal):
    """brain/internal/meta (retained)."""

    correlation_id: str
    zones: list[ZoneDecl]
    sensors: list[SensorDecl]
    actuators: list[ActuatorDecl]
    units: list[UnitDecl]


class InternalReading(_Internal):
    """brain/internal/reading."""

    correlation_id: str
    sensor_id: str
    zone_id: str
    type: str
    value: float
    unit: str
    origin: Literal["real", "simulated"] | None = None


class InternalCall(_Internal):
    """brain/internal/call."""

    correlation_id: str
    call_id: str
    zone_id: str
    category: str
    text: str | None = None


class InternalTrust(_Internal):
    """brain/internal/trust/{sensor_id} (retained). trust_score is null when Guardian gives none."""

    correlation_id: str
    sensor_id: str
    verdict: Literal["trusted", "suspicious", "rejected"]
    trust_score: float | None
    reason: str

    def to_json(self) -> str:
        # trust_score stays in the message as null (consumers expect the field)
        return self.model_dump_json()


class InternalQuarantine(_Internal):
    """brain/internal/quarantine/{device_id} (retained)."""

    correlation_id: str
    device_id: str
    state: Literal["quarantined", "released"]
    reason: str


class InternalAck(_Internal):
    """brain/internal/ack: Twin acknowledgement, actuator state or unit state.

    correlation_id = the Brain command id this acknowledges (result set),
    or the Twin message id for a plain state update (result absent).
    """

    correlation_id: str
    message_id: str
    kind: Literal["actuator", "unit"]
    target_id: str
    result: Literal["done", "rejected", "failed"] | None = None
    state: str | None = None
    status: str | None = None
    zone_id: str | None = None
    reason: str | None = None


class InternalLayerStatus(_Internal):
    """brain/internal/layer-status/{layer} (retained)."""

    layer: Literal["twin", "guardian"]
    status: Literal["online", "offline", "degraded"]
    reason: str | None = None


class BrainError(_Internal):
    """brain/error body (BR-5)."""

    source: Literal["brain"] = "brain"
    code: Literal["E_FORMAT", "E_AUTH", "E_STALE", "E_REPLAY", "E_UNKNOWN_ID", "E_RANGE"]
    topic: str
    message_id: str | None = None
    detail: str


class BrainStatus(_Internal):
    """brain/status body (BR-4)."""

    source: Literal["brain"] = "brain"
    status: Literal["online", "offline"]


class TimelineEvent(_Internal):
    """brain/event body (BR-3)."""

    source: Literal["brain"] = "brain"
    correlation_id: str | None = None
    event: str
    incident_id: str | None = None
    summary: str


# --- Internal (published by the LLM module and the rule fallback) --------------------


class ProposalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    action: str
    params: dict[str, str | int | float | bool] = {}


class Proposal(_Internal):
    """brain/internal/proposal: actions proposed for one incident (origin llm or rules)."""

    correlation_id: str
    incident_id: str
    origin: Literal["llm", "rules"]
    actions: list[ProposalAction]
    justification: str


class LlmStatus(_Internal):
    """brain/internal/llm-status (retained, Last Will offline)."""

    status: Literal["online", "offline"]
    model: str | None = None
    reason: str | None = None


# --- Internal (published by detection, scoring and output) --------------------------

IncidentType = Literal["flood", "fire", "accident", "traffic_jam", "power_problem"]
Severity = Literal["low", "medium", "high", "critical"]
# the one ordering of severities (higher = more severe): every module compares through this
SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class IncidentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str                      # sensor id or call id
    kind: Literal["reading", "call"]
    value: float | None = None
    unit: str | None = None
    category: str | None = None         # call category
    trust: float                        # weight used by detection: 0 (rejected) .. 1


class Incident(_Internal):
    """brain/internal/incident: opened / updated / closed (team plan 02, 4.4)."""

    correlation_id: str                 # external message that started the chain
    incident_id: str
    type: IncidentType
    zone_id: str
    status: Literal["opened", "updated", "closed"]
    confidence: float                   # 0..1, already includes Guardian's trust
    severity: Severity
    verified: bool                      # false when Guardian was offline during detection
    evidence: list[IncidentEvidence]
    reason: str | None = None           # why it opened / changed / closed (timeline, report)
    # what the case memory says (not evidence: it is derived from the same sensors, so the safety
    # rules never count it as an independent source), e.g. "looks like a real fire: 14 of 15 ..."
    case: str | None = None


class CaseMatch(_Internal):
    """brain/internal/case: the case memory's verdict for one zone (src/memory).

    The zone's live readings over the last window, encoded like the library's cases, and the
    vote of the k most similar cases. Detection uses it as a support signal (fire) or to hold
    down smoke readings (nuisance, e.g. cooking)."""

    correlation_id: str | None = None   # newest reading that went into the window
    zone_id: str
    incident_type: IncidentType         # what the library is about (fire)
    verdict: Literal["quiet", "nuisance", "fire"]
    share: float                        # votes for the verdict / k
    k: int
    votes: dict[str, int]               # verdict -> neighbours
    sources: dict[str, int]             # what the neighbours were (e.g. toast: 9, stir-fry: 6)
    features: dict[str, float]          # the window, in the library's units
    sensors: list[str]                  # sensors read
    library: str                        # e.g. NIST smoke alarm tests (TN 1947)
    summary: str                        # one line for the timeline and the LLM


class DecisionAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    origin: Literal["llm", "rules"]
    score: float


class Decision(_Internal):
    """brain/internal/decision: the actions chosen for an incident (team plan 04, section 6)."""

    correlation_id: str
    incident_id: str
    chosen_proposal: str
    origin: Literal["llm", "rules"]
    actions: list[ProposalAction]
    score: float
    alternatives: list[DecisionAlternative] = []
    risk: Literal["low", "medium", "high"]
    evidence_sensors: list[str] = []
    mode: Literal["normal", "safe"]     # safe = Guardian offline
    explanation: str                    # one line for the timeline


class Outcome(_Internal):
    """brain/internal/outcome: what happened to one action of a decision (team plan 05, section 5)."""

    correlation_id: str
    incident_id: str
    decision_id: str
    target_id: str
    action: str
    status: Literal["sent", "refused", "acknowledged", "failed"]
    command_id: str | None = None
    rule: str | None = None             # safety rule that refused it (S1..S6)
    detail: str | None = None


# --- External bodies published by the output module -------------------------------


class BrainCommand(_Internal):
    """brain/cmd/{target_id} (BR-1), sealed for the Twin. Format accepted by the mock Twin."""

    source: Literal["brain"] = "brain"
    correlation_id: str                 # chain id (external message that started it)
    target_id: str
    action: str
    parameters: dict[str, str | int | float | bool] = {}


class BrainAlert(_Internal):
    """brain/alert: a problem the Brain wants Guardian and the operators to know about."""

    source: Literal["brain"] = "brain"
    correlation_id: str | None = None
    alert: Literal["actuator_not_responding", "actuator_no_effect", "command_rejected"]
    target_id: str
    detail: str


INTERNAL_MODELS: dict[str, type[_Internal]] = {
    "internal-meta": InternalMeta,
    "internal-reading": InternalReading,
    "internal-call": InternalCall,
    "internal-trust": InternalTrust,
    "internal-quarantine": InternalQuarantine,
    "internal-ack": InternalAck,
    "internal-layer-status": InternalLayerStatus,
    "internal-proposal": Proposal,
    "internal-llm-status": LlmStatus,
    "internal-incident": Incident,
    "internal-decision": Decision,
    "internal-outcome": Outcome,
    "internal-case": CaseMatch,
    "brain-command": BrainCommand,
    "brain-alert": BrainAlert,
    "brain-error": BrainError,
    "brain-status": BrainStatus,
    "brain-event": TimelineEvent,
}
