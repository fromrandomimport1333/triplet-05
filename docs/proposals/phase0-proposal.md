# Phase 0 proposal (summary)

Source: *City Brain Convergence: Solution Proposal* (Phase 0, 10 pages, PDF not committed).
Written **before** the layer assignment, so it designs all three layers.

## Main ideas

- **Contract first.** The contract between layers matters as much as any layer: OpenAPI + JSON
  Schema for every message (`reading_id`, `sensor_id`, `type`, `value`, `unit`, `timestamp`,
  `signature`, `nonce`), an MCP tool manifest, and an integration guide for strangers.
- **Twin.** Pluggable signal generators per sensor with `inject_anomaly(sensor_id, mode)`
  (spike, freeze, replay, drift, drop-to-zero; freeze and replay kept separate). SUMO/TraCI for
  traffic as an optional upgrade behind the same interface. Actuators are state-changing tool
  calls whose effect shows in later readings.
- **City Brain.** Detection from sustained or corroborated patterns (flood: N readings above a
  zone threshold; fire: smoke + call; accident: call + flow drop; jam: sustained low speed).
  Decision by a **three-tier cascade**: rule table → decision cache → small "criticality" LLM for
  novel cases; if the LLM is unavailable, the most conservative rule action. Guardian gating
  (`get_trust_verdict(reading_id)` before every dispatch). Incident lifecycle open → dispatched →
  resolved, one dispatch per incident.
- **Guardian.** Same cascade adapted to trust verdicts: rules (replay, frozen, range, HMAC,
  EWMA/z-score drift, actuator mismatch) → verdict cache (only suspect/rejected verdicts reused)
  → LLM; fails closed. Containment: isolate, quarantine, rollback (never a fabricated value).
  No trained ML model, to avoid overfitting the Twin's synthetic data.
- **Stack.** Python 3.11 + FastAPI, WebSockets (chosen over MQTT to avoid a broker), FastMCP,
  Docker Compose, HMAC + timestamp/nonce with one key per pair of layers.

## Relation to the current plan

| Point | Status |
|---|---|
| Shared frozen contract across all teams; organizer adversarial scenarios; dates 22 Sep / 31 Oct / 1 Dec / 11 Dec | **Contradicted** by the Phase 1 Specifications (no imposed contract, scenarios written by each triplet, submission 7 Nov, CSTAM 14 Nov) |
| Sustained/corroborated detection, lifecycle, Guardian gating, HMAC per pair, schema validation, Docker Compose | Kept in the team plan |
| WebSockets instead of MQTT | Replaced: the plan uses MQTT over TLS |
| Rule → cache → LLM cascade | Replaced: LLM proposal + rule fallback, scored by the math model (`team-plan/04`) |
| R1–R4 (mocks, logs, timeline, collaboration) | Not covered by the proposal; covered by the plan |
