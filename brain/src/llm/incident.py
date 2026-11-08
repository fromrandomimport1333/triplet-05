"""The incident as the LLM module reads it (brain/internal/incident, overview section 3).

The graph team publishes and owns this format; this is a tolerant reader of it:
unknown fields are ignored, so additions on their side never break this module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_id: str
    kind: str                               # reading | call
    value: float | None = None
    unit: str | None = None
    category: str | None = None
    trust: float | None = None


class Incident(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    timestamp: str
    correlation_id: str
    incident_id: str
    type: str                               # flood | fire | accident | traffic_jam | power_problem
    zone_id: str
    status: Literal["opened", "updated", "closed"]
    confidence: float | None = None
    severity: str | None = None             # low | medium | high | critical
    verified: bool | None = None
    evidence: list[Evidence] = []
    case: str | None = None                 # the case memory's line (similar past cases), if any
