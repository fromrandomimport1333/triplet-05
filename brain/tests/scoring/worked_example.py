"""Worked example for the report: one flood, scored by the real service, every number printed.

    python -m tests.scoring.worked_example        (from the brain/ folder)

Flood at zone-01 (port, exposure high), severity medium, confidence 0.8, evidence: water-01
(trust 0.9) and a flood call (trust 1.0). The LLM answers after 3 s with valve-01 open,
signal-02 priority and the rescue unit; the rule fallback proposed valve-01 open and
signal-01 priority. Uses config/scoring.yaml as it is.
"""

from __future__ import annotations

import json

from src.common.log import EventLogger
from src.common.models import Proposal
from src.scoring.service import ScoringService
from tests.scoring.helpers import T0, Bus, Clock, call, incident, meta_message, proposal, reading


def run() -> tuple[ScoringService, dict, dict, list]:
    bus, clock = Bus(), Clock(T0)
    svc = ScoringService(bus, clock=clock, logger=EventLogger("worked-example", None, "ERROR"))
    svc.handle("brain/internal/meta", json.dumps(meta_message()), True)
    inc = incident("inc-0031", "flood", "zone-01", confidence=0.8,
                   evidence=[reading("water-01", trust=0.9, value=1.6), call("call-0011", "flood")])
    svc.handle("brain/internal/incident", json.dumps(inc))
    track = svc.tracks["inc-0031"]
    rules = track.rules
    clock.t += 3
    llm = proposal("inc-0031", [("valve-01", "open", {"opening": 0.8}), ("signal-02", "priority"),
                                ("unit-rescue-01", "dispatch")], pid="llm-000012")
    scores = [svc.score(track, rules), svc.score(track, Proposal.model_validate(llm))]
    svc.handle("brain/internal/proposal", json.dumps(llm))
    return svc, bus.decisions()[0], inc, scores


def main() -> None:
    svc, decision, inc, scores = run()
    m = svc.model
    s0 = scores[0]
    imp = m.zone_importance(svc.city.zones["zone-01"])
    print(f"Incident {inc['incident_id']}: {inc['type']} at {inc['zone_id']}, severity {inc['severity']}, "
          f"confidence {inc['confidence']}, waiting 3 s")
    print(f"P = {inc['confidence']} x {m.severity_weight(inc['severity']):g} x {imp:g} x (1 + 3/60) = {s0.priority:.3f}")
    print(f"trust of the evidence = mean(0.9, 1.0) = {s0.trust:.3f}; mode {s0.mode}")
    print()
    for s in scores:
        print(f"{s.origin} proposal {s.proposal_id}")
        print("  action                         eff   hops  cost   risk  risk_pen  score")
        for b in s.breakdown:
            print(f"  {b.action + ' ' + b.target_id:<30} {b.effectiveness:4.2f}  {b.hops!s:>4}  {b.cost:5.3f}  "
                  f"{b.risk:4.2f}  {b.risk_penalty:7.3f}  {b.score:6.3f}")
        print(f"  total = {s.score:.3f}")
    print()
    print("decision:", decision["explanation"])


if __name__ == "__main__":
    main()
