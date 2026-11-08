# Implementation review

Review of each implementation in the repository, as of 20 Oct 2026, against the team plan
(`../team-plan/`) and the Phase 1 requirements (`../challenge/phase1-requirements.md`).
Work is identified by branch and module, not by person. "Ran" means the command was executed
on a Windows 11 laptop (Python 3.14.4, MSVC 14.44); anything not executed is marked.

## Comparison

| | Input module (`main`) | Mock Twin (`mock/twin`) | Math library (`decision-math-library`) | LLM module (`llm/local-service`) |
|---|---|---|---|---|
| Plan item | `team-plan/01` | `team-plan/06` (Twin part) | `team-plan/04` (scoring + rule fallback) | `team-plan/03` |
| Merged in `main` | yes (PR #1) | no (PR #2 open) | yes (PR #3) | no (PR) |
| Language / runtime | Python, paho-mqtt, pydantic | Python, reuses `src/common` | C99 + CMake, cffi bindings, FastMCP | Python, stdlib HTTP to Ollama |
| Talks MQTT | yes (`brain/internal/*`, `brain/error`, `brain/event`, `brain/status`) | yes (Twin topics, sealed envelopes) | **no** | yes (`brain/internal/proposal`, `llm-status`, `brain/event`) |
| Broker-free tests | 74 passed | 51 passed (+74) | 95 Python + 12 C suites passed after workarounds | 70 passed (+74) + 1 live |
| Matches the plan's contract | yes | mostly (command format provisional) | **no** (other scope) | yes (proposal format of `team-plan/00` §3) |
| Completeness vs its plan | high | partial | low for the plan; high for its own scope | first milestone complete |
| Main risk | MQTT session / compose details | missing Guardian mock + scenario runner | portability, explainability, not wired | edge-device specifics, context API not built yet |

## Input module (`main`, `brain/src/input/`, `brain/src/common/`)

**Strengths**
- **Separation of logic and transport**, exactly what the jury checks: `InputService.handle(topic, payload, retained)` takes a `publish` callable and an injectable `clock` (`service.py`), so all 7 checks are tested without a broker; `__main__.py` only wires MQTT.
- The 7 checks run in the documented order with stable error codes (`checks.py`), each with valid and invalid tests (`tests/input/test_checks.py`); the HMAC test vector is covered (`test_envelope.py`).
- Thoughtful edge cases, documented in `brain/README.md`: retained and Last-Will messages skip freshness/replay but keep HMAC/format/ID checks; Guardian marked offline after 30 s of silence (`LayerTracker`); `seq` restarts accepted after `online`; clock drift logged.
- Everything partner-specific is configuration: `config/topics.yaml`, `field_map` and `verdict_aliases` in `config/input.yaml`, keys in `config/security.yaml`.
- `brain/error` is rate-limited; a measured performance test (`test_performance.py`, ≥ 200 msg/s, < 5 ms).
- Shared code other modules reuse: `BrainMqtt`, `seal`/`seal_multi`, models, JSON-Schema generation with a `--check` mode (`src/common/schemas.py`, used by `tests/test_contracts.py`).

**Weaknesses / to verify**
- `BrainMqtt` uses MQTT 3.1.1 with `clean_session=True` (`common/mqtt.py`): QoS 1 messages sent while a module is disconnected are lost. Retained state comes back, but non-retained ones (incidents, proposals, decisions) do not. Acceptable for the demo if known; a persistent session would fix it.
- `config.load` is `lru_cache`d: settings cannot change without restarting (fine, but worth knowing for tuning sessions).
- Root `docker-compose.yml` builds `./twin`, `./brain`, `./guardian`, but no `Dockerfile` exists yet, so `docker compose up` fails today.
- `.github/CODEOWNERS` uses placeholder handles (`@twin-team`…); the spec relies on CODEOWNERS to show each team's work.
- Models for the other modules' messages (incident, decision, outcome) are not in `common/models.py` yet; each owning team must add them so schemas exist in `contracts/`.

## Mock Twin (`mock/twin`, `brain/mocks/twin/`)

**Strengths**
- Clean layering: a pure, seeded simulator (`core.py: Twin`), a `Mapper` that builds wire bodies from the Brain's own `config/topics.yaml` (`messages.py`), a service that seals and handles commands (`service.py`), MQTT wiring in `__main__.py`.
- Reuses the Brain's envelope/topic/id code, so formats cannot drift; `tests/mocks/conftest.py` feeds its output into the real `InputService`. A throwaway cross-check accepted every scenario's messages (0 errors), and each fault produced the expected code.
- Idempotent command handling, command age and HMAC checked, actuator effects feed back into readings, a full unit lifecycle, 17 fault-injection modes.

**Weaknesses**
- One zone, 6 sensors, 3 actuators, 3 units (constants in `core.py`) versus the planned 5–6 zones: B5, B8 and B11 cannot be played.
- No rush-hour pattern (B1); one scenario at a time; calls are template text only (`core.py: _generate_truth`), so no free text or injection for B10/B11; call ids repeat across restarts.
- Fault modes `delay` and the default `old` are reported by the input module as `E_REPLAY` (`seq` goes backwards), not as the README states; `Twin.timestamp` is simulated time, so manual stepping yields `E_STALE`.
- `lost_ack` drops only the first ack (no true `no_ack`); quarantine rejection reason is `device_unavailable`, not `quarantined`.
- Missing pieces of `team-plan/06`: Guardian mock (without it the input module marks Guardian offline after 30 s, so every run is in safe mode), scenario YAML + `./scenarios/run <id>` with PASS/FAIL, B1–B12, compose service.
- Command body format is local and provisional; must be agreed with the output module.

## Math library (`decision-math-library`, `brain/src/{include,src,bindings,mcp_server}`)

**Strengths**
- Careful C99 API: status codes everywhere, no global state, validated finite inputs (`src/internal.h`).
- Strong testing: unit, property (70,493 random cases, 0 failures), adversarial and golden-vector tests shared by C and Python; assignment checked against brute force.
- The "one falsified source cannot cross the action threshold" bound is derived, documented and tested: a good argument for the fake-flood scenario.
- MCP tools are read-only; typed Python wrappers.

**Weaknesses**
- **Not the planned component.** No `P × effectiveness − cost − risk_penalty` scoring, no rule fallback, no `config/rules.yaml` / `scoring.yaml`, no MQTT, no proposal/decision messages; nothing in `src/scoring` or `src/rules`.
- **Windows/MSVC build defects** (found by building): static and import libraries both named `citybrain.lib` (ninja conflict); the DLL exports nothing (`CITYBRAIN_BUILD_SHARED` set on the wrong target); the binding hard-codes `libcitybrain.a`; tests fail under `/WX` (C4189, C4996); the benchmark uses POSIX `clock_gettime`. With workarounds, everything passes.
- Requires a C toolchain + CMake in Docker and on the demo laptop, against the plan's "Python 3.11+ for every module".
- The cffi `cdef` is a hand copy of the headers (two sources of truth).
- Hard to explain in 30 s (log-odds fusion, CUSUM, MDP, Gaussian plume), which `team-plan/04` names as a weakness; MDP, physics, calibration and cache have no consumer in the plan; constants live in code; MCP graph tools always use the 4-node example graph.
- Benchmark figures in its docs (e.g. ns per call) come from another machine and were not reproduced here.

## LLM module (`llm/local-service`, `brain/src/llm/`)

Reviewed with the same criteria; this is the module prepared in this session (see
`../report/brain-llm.md` on that branch for details).

**Strengths:** same service pattern as the input module (pure `LlmService`, injected clock and
publisher); local context from retained topics so it runs without the graph API; three injection
defences (data block, relevant device types per incident type, schema enums) plus strict
validation; hard 8 s budget with streamed generation; latest-update-wins queue; Last Will status;
mock LLM in the four required modes; other open incidents nearby shown to the model (B11); model
build pinned; re-warm-up after the runtime recovers. Measured on the RTX 5060 laptop with
`qwen2.5-coder:7b`: on the 6 Test Book cases 100 % valid and correct, median 2.1 s; on a
generated 62-case set (×3 runs) 98 % valid, 98 % correct, all 21 injection runs safe, max 2.8 s;
three simultaneous incidents answered within budget over MQTT; killing and restarting Ollama
under a running module gave an instant hand-over to the rules, then a correct answer 2.4 s after
recovery.

**Weaknesses:** one request at a time on the GPU (beyond three simultaneous incidents the least
severe go to the rules); `graph_url` and the `/context` JSON shape are assumptions until the
graph API exists; action semantics (`action_hints`) and unit actions (`unit_actions`) must be
confirmed with the Twin and output teams; one generated case (a jam with an injected call) gets
no proposal because the model ignores the whole incident along with the call, which is safe but
leaves the jam to the rules.

## Recommendation: how to combine the work

1. **Keep `main`'s architecture as the backbone.** The input module's pattern (pure service +
   thin MQTT wiring + config + schemas) is what the jury rewards; every module should follow it,
   as the mock Twin and LLM module already do.
2. **Merge `mock/twin` soon, then extend it** rather than starting over: multi-zone city from a
   `mocks/city.yaml`, rush hour, free-text and injection calls (B10, B11), a Guardian mock (most
   urgent, otherwise every run is in safe mode), then the scenario runner. Fix the `delay`/`old`
   README statements or the modes themselves.
3. **Contain `decision-math-library`, now on `main` (PR #3).** First make the test suite run without
   it: `tests/python/` should skip when `citybrain` is not built (`pytest.importorskip`), since
   today `python -m pytest` fails for everyone who has no C toolchain. Then move the CMake
   project out of the Python package root (e.g. `brain/native/citybrain/`), fix the portability
   defects, and treat it as an **optional** accelerator/explanation tool. Build the planned
   `src/scoring` and `src/rules` services in Python first (they unblock the end-to-end run of
   26 Oct); reuse the library's ideas where they help: capped fusion as the explanation of the
   trust penalty (B3/B4), `assignment.solve` for B5.
4. **Merge `llm/local-service`** after review by another team (per `team-plan/00` §5); it only
   adds files plus two additive model/topic entries.
5. **Fix the shared blockers**: Dockerfiles for the compose file, real CODEOWNERS handles, the
   incident/decision/outcome models in `common/models.py`.
6. **Decide as a team** whether the math library replaces or sits under the planned scoring
   formula before the decision format is used by the output module.
