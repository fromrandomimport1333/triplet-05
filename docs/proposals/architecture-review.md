# Architecture review of the City Brain (summary)

An independent review (4 Oct 2026) of the Phase 0 proposal and the design brief against the
Phase 1 Specifications. The full document is kept outside the repository; this is its summary.

## Findings

- Both earlier documents were written without the Phase 1 rules: no imposed contract, scenarios
  written by the triplet, R1–R4 deliverables, 7 Nov submission.
- 25 of the 50 points are interoperability and security, so the design should centre on a clean
  separation of logic and transport, config-driven partners, standard protocols, validation and
  explicit trust handling.
- Weak points found in the Phase 0 City Brain: an LLM tier for "novel incidents" that no input
  can reach (detection only produces known types), a decision cache with nothing to cache, a
  synchronous per-reading trust RPC, trust checked on the triggering reading only, "dispatch the
  conservative action" when the LLM fails, no actuator choice, one action per incident, no effect
  check, no degraded mode.
- The design brief's strongest ideas: the reading gate, the safety verifier as the only path to
  actuators, capped evidence per source, decision logs with reasons, testable guarantees. Its
  heaviest parts (MDP, Dempster-Shafer, Z3, calibration engine, MCP advisor) were judged too
  costly for the timeline.

## Recommendations that the team plan adopted

MQTT as the bus with retained state and Last Will; event-driven modules; detection with EWMA
baselines and persistence; one incident per zone and type; multi-action responses; quarantine
handled at actuator choice; Guardian-offline safe mode where risky actions need more evidence;
a Test Book scenario per behaviour (calm day, fake flood, quarantined valve, Guardian offline,
LLM offline); everything in configuration.

## Where the team plan differs

- **LLM role.** The review recommended keeping the LLM out of the decision path (explanations
  only). The team plan makes the LLM a **proposer** whose output is validated, scored against the
  rule fallback and passed through the safety rules. With those three layers and the rule
  fallback, an LLM failure costs quality, not safety; the LLM module's design follows the plan.
- **Granularity.** The review proposed one Python service; the plan splits the Brain into modules
  talking over `brain/internal/*` topics so each sub-team can build and test alone.
