# City Brain design brief (summary)

Source: *City Brain Layer: Design Brief* (8 pages, PDF not committed). Written after the layer
assignment, for the City Brain only. Numbers in it are stated as illustrative.

## Pipeline (as proposed)

1. **Reading gate:** schema validation (unknown fields ignored), HMAC, single-use nonce,
   timestamp window, registry mapping partner sensor IDs to internal IDs, unit/range
   normalisation, only typed values move on. **1b. Verdict parser:** strict; missing, late,
   stale or unknown verdict counts as suspect.
2. **Incident detection:** flood, fire, accident, jam patterns; CUSUM and EWMA change detection;
   always sustained or corroborated.
3. **Correlation graph:** relations raises / lowers / causes between incidents, bounded odds
   multipliers (e.g. accident → jam ×3), jam attached to its accident as a child, implausible
   combinations flagged to Guardian, a floor so "lowers" never blocks strong evidence.
4. **Decision cascade:** rule table → decision cache → evidence fusion (log-odds with capped
   likelihood ratios), expected-cost action ladder, Hungarian assignment, MDP policy (stretch),
   **LLM advisor** reading the same functions through read-only **MCP tools** and choosing only
   from the shortlist; no match → most conservative rule action.
5. **Safety verifier:** the only path to actuators (whitelist, target exists, safe bounds, trust
   floor, no open dispatch, no conflict, atomic check-and-set).
6. **Lifecycle:** open → dispatched → resolved / flagged, watchdog timeouts, one dispatch per incident.
7. **Calibration:** settings only, at lifecycle boundaries, bounded, from trusted data, shadow
   replay before applying, versioned with rollback.
8. **Formal verification:** Z3 for the rule table and the safety inequality, model checking of
   the lifecycle, property tests, a hijacked-LLM harness.
9. **Observability:** decision log with evidence trail, reason codes, replay.

Guarantees: G1 no action without trust above the floor · G2 at most one dispatch per incident ·
G3 one falsified evidence group cannot trigger an irreversible action · G4 a hijacked LLM cannot
leave the admissible set · G5 calibration stays in bounds · G6 odd partner formats never crash
the Brain. LLM security: no raw partner text to the model, read-only tools, schema-constrained
output, shortlist only, local pinned model, timeout.

Open question raised by the brief: what the Brain does when Guardian is unreachable for long.

## Relation to the current plan

| Brief | Team plan |
|---|---|
| Reading gate, verdict parser | Input module (`team-plan/01`), implemented on `main` |
| Detection with EWMA, sustained/corroborated | Graph team, detection (`team-plan/02`) |
| Correlation graph | Graph DB + context API (`team-plan/02`); compound incident B11 handled by the LLM |
| Fusion, expected cost, Hungarian, MDP, calibration | Not in the plan; a C library implementing them exists on branch `decision-math-library` (see `../repository/implementation-review.md`) |
| LLM advisor choosing from a shortlist via MCP | Replaced: the LLM proposes actions directly from a compact context; the math model scores them against the rule fallback (`team-plan/03`, `04`) |
| Safety verifier | Output module safety rules S1–S6 (`team-plan/05`) |
| Guardian unreachable | Answered: safe mode, risky actions need two different sensor types (S6) |
| Z3 / model checking | Not planned |
