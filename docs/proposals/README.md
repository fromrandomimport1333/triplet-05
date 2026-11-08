# Earlier design documents

Three design documents were written before the current module plan (`../team-plan/`). They are
kept here as **summaries** (the original PDFs are not committed) so that the reasoning behind
the plan stays visible. **The team plan is the reference** where they disagree.

| Document | When / scope | Status |
|---|---|---|
| [Phase 0 proposal](phase0-proposal.md) | Before layer assignment; covers all three layers | Superseded by the team plan; several assumptions contradicted by the Phase 1 Specifications |
| [City Brain design brief](design-brief.md) | After assignment; City Brain only | Many ideas carried into the team plan; several advanced components not planned |
| [Architecture review](architecture-review.md) | 4 Oct 2026; independent review of the two documents above against the Phase 1 Specifications | Input to the team plan; differs from it on the LLM's role (see that file) |

## How the ideas flowed into the current plan

| Idea | Phase 0 | Design brief | Team plan (current) |
|---|---|---|---|
| Sustained / corroborated detection, never one reading | yes | yes (+ CUSUM/EWMA) | yes, EWMA + 3 readings (`02-graph` 4.1) |
| Guardian trust gates actions | per-reading RPC | strict verdict parser | trust weights in detection, trust in scoring, safety rules S2/S3/S6 |
| Incident lifecycle, one dispatch per incident | yes | yes | yes (`02-graph` 4.4) |
| Decision method | rules → cache → LLM cascade | rules → cache → fusion + LLM advisor | **LLM proposal + rule fallback, scored by the math model, then safety rules** |
| LLM role | tier 3 for novel cases | advisor choosing from a shortlist, via MCP tools | proposes actions on every incident, local model, 8 s budget (`03-llm`) |
| Single path to actuators with safety checks | — | safety verifier | output module, safety rules S1–S6 (`05-output`) |
| Transport | WebSockets | (not specified) | MQTT over TLS, HMAC envelope |
| Mocks, logs, timeline (R1–R3) | not covered | not covered | mock module, output B (timeline, logs) |
