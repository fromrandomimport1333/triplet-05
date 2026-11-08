# Part 5: Collaboration process

**Tools and workflow.** One GitHub repository for the triplet, one folder per layer. Inside the City
Brain, the work was split by module (input, detection and graph, LLM, scoring and rules, output and
timeline, mocks), each with its own plan in `docs/team-plan/`. The internal message formats were
frozen early (`docs/team-plan/00`, section 3) and turned into code contracts: pydantic models in
`brain/src/common/models.py`, from which the JSON Schemas of `brain/contracts/schemas/` are generated;
a test fails if a schema is out of date. Every module follows the same pattern (a pure service tested
without a broker, a thin MQTT entry point, settings in `config/`), so modules built in parallel could
be merged without integration surprises. One branch per task, merged through pull requests
(#1 input, #2 mock Twin, #3 math library, #11 LLM, #12 documentation, #13 test fix); problems found in review
were filed as issues with evidence and a proposed fix (#4–#10).

*[To complete by the team: meeting rhythm, communication channels, and coordination with the Twin and
Guardian teams.]*

**How a partner failure or change was handled.**

- *No interface specification in the repository.* The input module had been written against an
  interface draft that was never committed. The implemented checks became the specification:
  `brain/INTERFACE.md` documents every topic with an example, error codes, authentication, and how
  to point the Brain at another partner.
- *Partner layers not available for integration.* The City Brain was built and tested against
  stand-ins that speak exactly the agreed protocol (R1): a mock Twin (six zones, sensors, actuators,
  units, faults, calls with free text) and a mock Guardian (verdicts from real checks, quarantine,
  forced modes). Replacing them by the real layers changes only the broker configuration.
- *A partner going down at run time.* Each layer publishes a status with a Last Will. The Brain
  switches to safe mode when the Guardian is offline (B4), refuses commands while the Twin is
  offline (B13), falls back to its rules when the local LLM is offline (B12), and treats a Twin
  restart as a new city state.
- *A component changing under us.* The local LLM runtime updated itself during development; the
  model build is pinned by digest, and a different build makes the LLM report itself offline instead
  of answering with an unbenchmarked model.

**Evidence of a successful joint run.** All Test Book scenarios ran end to end on the live system
(broker with TLS and access control, every City Brain module, mock Twin and mock Guardian, local
LLM), started by one command each. Each run left one log file in `logs/` (R2) with every message
observed and the PASS/FAIL result of each expectation; the summary is in Part 4.
