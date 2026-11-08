# brain

How to run this layer alone.

## Setup

Python 3.11+, Docker (for the broker). From the `brain/` folder:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Linux/macOS: .venv/bin/pip
python tools/setup.py                            # once: CA + broker certificate, HMAC keys, broker accounts
docker compose -f ../docker-compose.yml up -d broker   # Mosquitto, TLS only, topic ACL
python run.py --mocks                            # every Brain module + mock Twin + mock Guardian
```

Then open the live timeline on http://127.0.0.1:8090 and play a scenario from the repository root:
`python scenarios/run.py B2`. Without a GPU or Ollama: `python run.py --mocks --mock-llm`.
Everything in one command instead (broker + Brain + mocks in containers): `docker compose up`.

`tools/setup.py` writes into `brain/secrets/` (git-ignored, never committed); paths are in
`config/security.yaml`:

```
secrets/ca.crt                     # triplet CA (give it to the Twin and Guardian teams)
secrets/broker/                    # broker certificate, key and password file (mounted by docker compose)
secrets/broker-password.txt        # password of the `brain` account (twin-, guardian-, tester-password.txt: the others)
secrets/keys/twin-brain.key        # HMAC key Twin-Brain (32 random bytes, hex)
secrets/keys/guardian-brain.key    # HMAC key Guardian-Brain
secrets/keys/guardian-ed25519.pub  # only if guardian_signature.mode = ed25519
```

`python tools/setup.py --host 192.168.1.20` adds a name or address to the broker certificate (other
laptops connecting over the demo network); `--renew-certs` recreates the certificates only.

## Modules

| Module | Run | Code | Team doc |
|---|---|---|---|
| Input (front door) | `python -m src.input` | `src/input/` | `01-input` |
| Detection (readings and calls -> incidents) | `python -m src.detection` | `src/detection/` | `02-graph` |
| Case memory (fire or cooking? similar past cases) | `python -m src.memory` | `src/memory/` | `vector-storage/` |
| Graph (city memory, context API on :8080) | `python -m src.graph` | `src/graph/` | `02-graph` |
| LLM (local model, proposals) | `python -m src.llm` | `src/llm/` | `03-llm` |
| Math (scoring + rule fallback, decisions) | `python -m src.scoring` | `src/scoring/`, `src/rules/` | `04-math-model` |
| Mock LLM (no model needed) | `python -m mocks.mock_llm [scripted\|slow\|offline\|invalid]` | `mocks/mock_llm/` | `03-llm` |
| Output (safety rules, signed commands) | `python -m src.output` | `src/output/` | `05-output` |
| Timeline (live screen, run logs) | `python -m src.timeline` | `src/timeline/` | `05-output` |
| Twin mock (test stand-in) | `python -m mocks.twin` | `mocks/twin/` | see `mocks/README.md` |

Every module reads `config/*.yaml`. To use another config folder (e.g. a local broker without TLS), set `BRAIN_CONFIG_DIR=/path/to/config`.

## Tests

```bash
.venv/bin/python -m pytest              # all tests
.venv/bin/python -m pytest tests/input  # input module only
.venv/bin/python -m src.common.schemas  # regenerate contracts/schemas/ after changing a model
```

## Shared code (`src/common/`)

| File | Use |
|---|---|
| `mqtt.py` | `BrainMqtt`: TLS / mTLS, auto-reconnect and re-subscribe, optional Last Will |
| `envelope.py` | `seal`, `seal_multi`, `verify_hmac`, `verify_ed25519`: the envelope of the interface spec |
| `models.py` | pydantic models of external bodies and of the internal messages |
| `config.py` | YAML loader, paths relative to `brain/` |
| `log.py` | JSON-lines logger (`logs/`) |
| `ids.py` | `in-000001` style ids |

---

## Input module

The front door of the Brain. Every Twin and Guardian message goes through 7 checks, in this order, stopping at the first failure:

| # | Check | Error |
|---|---|---|
| 1 | Envelope: JSON with `v`, `body`, `hmac` / `hmacs` | `E_FORMAT` |
| 2 | Signature: HMAC-SHA256 over the body text (`hmac` or `hmacs.brain`); Ed25519 for Guardian if enabled | `E_AUTH` |
| 3 | Freshness: timestamp within +-10 s | `E_STALE` |
| 4 | Replay: id not seen in the last 60 s; `seq` increasing per device if present | `E_REPLAY` |
| 5 | Format: fields and types; topic and body agree; `source` matches the topic | `E_FORMAT` |
| 6 | Known IDs: declared in `twin/meta` (zone, type of the sensor too) | `E_UNKNOWN_ID` |
| 7 | Range: value within the sensor's `min` / `max` | `E_RANGE` |

Accepted messages are republished on `brain/internal/…` (formats in `team-docs` overview, section 3; schemas in `contracts/schemas/`). Rejected ones are logged and published on `brain/error` (rate-limited).

**Published:**

| Topic | Retained | When |
|---|---|---|
| `brain/internal/meta` | yes | city description received |
| `brain/internal/reading` | no | valid reading |
| `brain/internal/call` | no | valid emergency call |
| `brain/internal/trust/{sensor_id}` | yes | Guardian verdict (`suspect` mapped to `suspicious`; `trust_score` or `null`) |
| `brain/internal/quarantine/{device_id}` | yes | quarantine / release |
| `brain/internal/ack` | no | Twin acknowledgement, actuator state, unit state |
| `brain/internal/layer-status/{twin,guardian}` | yes | layer online / offline / degraded |
| `brain/error` | no | rejected message (sealed for Twin and Guardian) |
| `brain/event` | no | city description received, layer online / offline, device quarantined / released |
| `brain/status` | yes | `online` on connect, `offline` as Last Will and on clean stop |

**`brain/internal/ack`** (not in the overview yet):

```json
{"id": "in-000612", "timestamp": "2026-11-14T10:22:04.420Z", "correlation_id": "brain-000042",
 "message_id": "twin-000205", "kind": "actuator", "target_id": "valve-port-up",
 "result": "done", "state": "open_80"}
```

`correlation_id` is the Brain command id when the Twin acknowledges a command (`result` present), otherwise the Twin message id. Unit messages add `status` and `zone_id`. Absent fields are omitted.

**Design choices to know:**

- **Retained messages skip freshness and replay.** The broker hands them over again on every (re)subscribe, so they are old by design. They still need a valid HMAC, format and IDs. Without this, a restarted Brain would refuse its own city description (IT-1).
- **Status topics skip freshness and replay.** A Last Will is written when the client connects, so it is always old when the broker fires it.
- **Guardian online / offline:** offline if `guardian/status` says so, **or** if no live verdict or quarantine has arrived for 30 s (`layers.guardian_silence_s`). Retained verdicts don't count as a sign of life.
- **`seq` restarts** are accepted when the Twin (or Guardian) comes back `online`.
- **Field-name differences** found at a joint run go in `config/input.yaml` → `field_map` (e.g. `event_id: id`), not in code. Topic names are in `config/topics.yaml`.
- A clock difference over 2 s is logged as `clock_drift` (ask the Twin to use NTP).
- Every 30 s the module logs `input_stats`: messages/s and average time per message.

**Measured:** about 0.07 ms per message through a real TLS broker, and well over 200 messages/s (unit test `tests/input/test_performance.py` checks < 5 ms and >= 200 msg/s).

**Switching to mTLS / Ed25519** (if agreed with Guardian): set `tls.client_cert` / `tls.client_key`, and `guardian_signature.mode: ed25519` with the public key path, in `config/security.yaml`. No code change.

---

## LLM module

A local LLM proposes actions for each incident. It only **proposes**: the math module scores the proposal against the rule fallback, and the output module applies the safety rules. If the LLM is slow, offline or answers badly, the rule fallback's proposal is used.

```
brain/internal/incident (opened / updated) ─▶ context ─▶ prompt ─▶ local LLM (Ollama) ─▶ validate ─▶ brain/internal/proposal
                                              ▲                                              │ refused
          meta, quarantine, ack, call ────────┘ (+ graph GET /context, optional)             └─▶ brain/event llm_invalid_output
```

**Consumes:** `brain/internal/incident`, `meta`, `call`, `ack`, `quarantine/+`, and optionally the graph `GET /context/{incident_id}` (`context.graph_url`).
**Publishes:**

| Topic | Retained | When |
|---|---|---|
| `brain/internal/proposal` | no | valid answer, `origin: llm` (schema `contracts/schemas/internal-proposal.schema.json`) |
| `brain/internal/llm-status` | yes | `online` / `offline` (Last Will, and on every health change) |
| `brain/event` | no | `llm_proposal`, `llm_timeout`, `llm_invalid_output`, `llm_skipped`, `llm_online`, `llm_offline` |

**Setup (edge device = the demo laptop):** install [Ollama](https://ollama.com), then `ollama pull <model>` for the model in `config/llm.yaml` (`backend.model`). The module warms the model up at start and keeps it loaded (`keep_alive`). No internet is needed once the model is pulled. Use `127.0.0.1`, not `localhost`, in `backend.url`: on Windows, `localhost` tries IPv6 first and adds about 2 s to every call.

**Benchmark** the candidates on the device (writes the table for the report):

```bash
python -m src.llm.bench --models qwen2.5:3b,llama3.2:3b,qwen2.5-coder:7b --repeat 10
```

Cases are in `tests/llm/cases.yaml` (B2, B5, B8, B10, B11 of the Test Book); each run goes through the real service (context, prompt, validation).

**Design choices to know:**

- **Time budget, hard.** The 8 s (`budget.answer_s`) start when the incident is received: queue wait, graph call and generation all count. The answer is streamed, and the connection is closed at the deadline, which also stops Ollama generating. Late answers are dropped with `llm_timeout`.
- **Latest version wins.** A newer update of the same incident replaces the queued request; a `closed` incident cancels it; an answer for an incident that changed meanwhile is dropped.
- **Context is built locally** from the retained internal topics, so the LLM works even if the graph API is down. The graph only adds related events and quarantined devices. The prompt stays under 1,500 characters (`context.max_chars`); related events, then older calls, then extra evidence are dropped first.
- **Only relevant devices are offered** (`relevant_targets` per incident type): on an accident the model never sees a valve, whatever a call says. Quarantined devices and busy units are left out.
- **Structured output.** The answer JSON Schema given to Ollama lists only this incident's devices and actions as enums, so the model cannot even produce an unknown id.
- **Strict validation anyway** (`validate.py`): known device, allowed action, not quarantined, at most 3 actions, bounded params and justification. In `strict` mode one bad action refuses the whole answer.
- **Call text is data.** It sits in a `<call_text>` block; `<` and `>` are neutralised so it cannot close the block, and the system prompt says it is never an instruction.
- **Action hints** (`action_hints`) tell the model what each action does (e.g. valve `open` drains the zone). They must match the Twin's simulation.
- **Everything is logged** (`logs/llm.jsonl`): prompt version, model, prompt, raw answer, latency.

**Mock LLM** (`python -m mocks.mock_llm <mode>`): the same service with a scripted backend, for teams without the GPU. `scripted` answers from `mock.playbook`, `slow` answers after 15 s (→ `llm_timeout`), `offline` never answers (→ `llm-status offline`), `invalid` returns broken JSON (→ `llm_invalid_output`).

---

## Detection module

Turns the stream of readings and calls into incidents: `opened`, `updated`, `closed`, never one message per reading.

```
reading ─▶ per-sensor baseline (EWMA) ─▶ abnormal? ─┐
call ──────────────────────────────────────────────┼─▶ per zone + type: weighted signals x Guardian trust ─▶ confidence ─▶ lifecycle ─▶ brain/internal/incident
trust/+, layer-status/guardian ─▶ weights ─────────┘                                                                    └─▶ brain/event
```

**Calls** of a zone and category are one source, capped (B7), over `calls.window_s` (120 s). The exception is `calls.by_category`: flood calls count for 60 min and are full at 2 callers. This is calibrated on 5,613 NYC 311 flood calls during Hurricane Ida (storm night: a median 19 min between calls of one ~1 km area; 51 % of flooded areas had a second call within an hour). `python tools/calibrate_calls.py` recomputes it from `vector-storage/data/nyc311/`.

**Consumes:** `brain/internal/meta`, `reading`, `call`, `trust/+`, `layer-status/+`, `case` (case memory). Zones, neighbours, sensors and alert levels all come from `meta`; no id is in the code.
## Case memory

"Which past cases look like this moment?": the dataset study of `vector-storage/` (NIST smoke alarm tests) running inside the Brain, offline.

```
readings (smoke, co, co2, temperature) ─▶ 1 value / s / sensor ─▶ last 60 s vs own baseline ─▶ 7 numbers ─▶ 15 nearest of 4,998 lab cases ─▶ vote
                                                                                                          └─▶ brain/internal/case ─▶ detection, timeline
                                                                                                          └─▶ brain/event case_fire / case_nuisance
```

- **Library:** `data/cases/nist_fire_windows.csv`, built by `python tools/build_cases.py --check` from `vector-storage/data/nist/nist_room_tests.zip`. It uses the same cleaning, windows, features and scales as the notebook, in the standard library only. `--check` reproduces the notebook's 98.7 % on the held-out group. Group 5 is kept out of the library; the mock Twin replays two of its tests (`mocks/twin/traces/fire.csv`, `cooking.csv`), so the live demo runs on cases the memory never saw.
- **Per zone** with a sensor of every `required` type (`config/memory.yaml`: smoke and CO; the mock city has them in zone-05 and zone-06). A zone without them is never assessed and keeps the plain behaviour.
- **The vote:** k = 15, Euclidean distance on signed-log features. A verdict needs `min_share` (60 %) of the votes. Falling smoke never starts a verdict (the tail of an episode). A *fire* verdict is never turned into *cooking*: the zone must be quiet first.
- **Detection** (`config/detection.yaml` → `incidents.fire.memory`):
  - *fire* adds a **support** source (it never opens an incident alone, and does not count for severity);
  - *nuisance* multiplies the zone's reading sources by 0.2, while calls still count in full;
  - readings alone in such a zone wait up to 25 s for a verdict before a fire opens;
  - the memory's line travels in the incident's `case` field. It is never in `evidence`, so S3 and S6 never count it as an independent source.
- **LLM:** the line appears in the prompt under *SIMILAR PAST CASES*.
- **Timeline:** a row on each verdict change, and the map badge *COOKING SMOKE, NOT FIRE*.

**Produces:** `brain/internal/case` (`CaseMatch`), `brain/event` (`case_fire`, `case_nuisance`).
**Consumes:** `brain/internal/meta`, `reading`, `trust/+` (readings Guardian rejected are left out).

## Math module (scoring + rule fallback)

One process, `python -m src.scoring`: the rule fallback (`src/rules/`) is called by the scoring service (`src/scoring/`), so a rule proposal always exists when the decision is taken, even with the LLM offline. Settings: `config/rules.yaml` (standard actions), `config/scoring.yaml` (every number of the score and the timing). Report paragraph: `docs/report/brain-scoring.md`.

```
incident (opened / updated) -> rule proposal (origin rules) -> wait for the LLM (8 s) -> check + allocate units -> score -> brain/internal/decision
                                                                ^ llm proposal, llm-status, llm_timeout / llm_invalid_output / llm_skipped (brain/event)
outcome failed / refused, unit free again, severity up / new evidence type -> decide again (decision_changed, new actions only)
```

**Consumes:** `brain/internal/meta`, `incident`, `proposal` (origin llm), `outcome`, `ack` (unit status and zone), `llm-status`, `quarantine/+`, `trust/+`, `layer-status/+`, and `brain/event` (LLM give-up events; sealed envelopes are checked with the outgoing keys of `config/security.yaml`).
**Publishes:**

| Topic | Retained | When |
|---|---|---|
| `brain/internal/incident` | no | incident `opened` / `updated` / `closed` (schema `contracts/schemas/internal-incident.schema.json`), ids `inc-0001`... |
| `brain/event` | no | `incident_opened`, `incident_updated`, `incident_closed`, `evidence_rejected` (sealed for Twin and Guardian) |

**Method** (all numbers in `config/detection.yaml`):

1. **Per sensor.** Running mean and variance (cumulative for the first 20 readings, then EWMA, weight 0.05). A reading is *bad* when it is at the sensor's `alert` level, or (after warm-up) more than 3 standard deviations away in the bad direction of its type (water, air, power, density: up; speed, flow: down). Three bad readings in a row make the sensor abnormal, three good ones (z < 2 and below 0.9 x alert) normal again. Only quiet readings (|z| <= 2) are learned, so a flood or a slow rush-hour ramp is never learned as normal. A sensor silent for 10 s stops counting.
2. **Per zone and incident type.** Each source (one sensor, or *all* recent calls of one category in the zone) gives `p = min(cap, weight x strength x trust)`; `confidence = 1 - prod(1 - p)`. Strength is 1 at alert level, else `z / 6`; for calls `count / 3`. Caps: 0.6 for one reading, 0.95 for a reading at alert level for 3 readings, 0.55 for calls, so one source alone stays low unless it is a sustained alert-level reading.

   | Incident | Primary signals | Support (only with a primary) | Gates |
   |---|---|---|---|
   | `flood` | water level (0.9), flood calls (0.6) | water in a neighbouring zone (x 0.3) | rising to open (unless a call confirms) |
   | `fire` | air quality (0.85), fire calls (0.6) | power spike (0.3), air in a neighbour | |
   | `accident` | sudden speed drop (0.45), accident calls (0.6) | density (0.2) | without a call capped at 0.45: a drop alone may be a jam |
   | `traffic_jam` | speed down >= 30 % for 20 s (0.7) | density (0.4), flow (0.3) | not during a city-wide pattern (>= 50 % of zones slow: rush hour) unless the zone is under half the city median; not when an accident explains it |
   | `power_problem` | power load (0.8) | | not when a fire is in the zone or a neighbour |
3. **Guardian trust:** trusted = `trust_score` or 1.0, suspicious = `trust_score` or 0.3, rejected = 0 (ignored, `evidence_rejected` on the timeline once), no verdict or Guardian offline = 0.6 and `verified: false`. Calls are not checked by Guardian: an incident with no reading evidence is never `verified`.
4. **Lifecycle:** open at confidence >= 0.5 on 2 checks in a row; one open incident per zone + type; `updated` on a new evidence source or a severity change, at most every 5 s (a lower severity only after 15 s); closed after 2 checks below 0.25 or with no live evidence. Checks run every second and when a sensor changes state or a call arrives. `correlation_id` = the first bad reading (or first call) of the chain.

**Severity:** low under 0.6 confidence; high when the value is 50 % over alert or 2 sources agree at >= 0.75; critical only with 3 sources, >= 0.9 and 50 % over alert.

**Ten fake calls (B7)** are one source capped at 0.55: one `low`, unverified incident at most, closed when the calls age out (120 s). A fake flood whose sensor Guardian rejects (B3) opens nothing.

---

## Graph module

The Brain's memory: the city map and every event, linked, in memory (stdlib only, no database server), with a small read-only HTTP API.

**Consumes:** `brain/internal/meta`, `reading`, `call`, `trust/+`, `quarantine/+`, `ack`, `layer-status/+`, `incident`, `proposal`, `decision`, `outcome`. **Publishes:** nothing on MQTT; HTTP on `http.host:http.port` (`config/graph.yaml`, default `127.0.0.1:8080`, the `context.graph_url` of the LLM module).

| Nodes | Links |
|---|---|
| zone, sensor, actuator, unit (from `meta`) | zone `NEXT_TO` zone, sensor `IN` zone, actuator `IN` / `CONTROLS` zone, unit `BASED_IN` zone |
| reading, verdict, call, quarantine | reading `FROM` sensor, verdict `ON` reading, call `IN` zone, quarantine `ON` device |
| incident, proposal, decision, action, ack | incident `IN` zone, incident `EVIDENCE` reading / call, proposal / decision `FOR` incident, decision `CHOSE` proposal, action `EXECUTES` decision, action `TARGETS` device, ack `CONFIRMS` action |

| Endpoint | Returns |
|---|---|
| `GET /context/{incident_id}` | incident, evidence, zone and neighbours, actuators (own zone first) with state and quarantine, free units, `quarantined`, `related` (incidents, calls, actions in the area, last 15 min); compact JSON under 1,500 characters |
| `GET /zone/{zone_id}` | neighbours, sensors with latest value and trust, actuators that control it, units in it, open incidents |
| `GET /actuators?zone_id=` | actuators that control the zone with state and quarantine (all without `zone_id`) |
| `GET /chain/{correlation_id}` | ordered steps reading -> verdict -> call -> incident -> proposals -> decision -> action -> ack |
| `GET /graph` | nodes and edges: the city + the 20 most recent incidents and what is linked to them (for a visualization) |
| `GET /health` | node counts per kind, dropped nodes, layer status |

Unknown ids and paths answer 404 with a JSON body.

**Design choices to know:**

- **Normal readings are not stored.** The sensor node keeps its latest value; the last 5,000 readings and verdicts wait in a ring. A reading becomes a node when it is at alert level, when Guardian rejects or suspects it, or when an incident uses it (its chain start and its evidence).
- **Linked by correlation_id.** Every event is indexed by the id of the external message that started its chain. Acks carry the Brain command id, so they are linked through the action that holds that `command_id`.
- **One action node per action of a decision**; the outcomes `sent`, `acknowledged`, `failed`, `refused` update its status and history.
- **Bounded memory:** a cap per kind (`caps`), the oldest node dropped with its links; the city is never dropped.
- **Thread safety:** one lock; MQTT callbacks write, HTTP threads read. Answers take about a millisecond (test: < 100 ms).
| `brain/internal/proposal` | no | `origin: rules`, on every opened / updated incident (ids `rules-...`) |
| `brain/internal/decision` | no | chosen actions, score, alternatives with scores, risk, evidence sensors, mode, one-line explanation (ids `math-...`) |
| `brain/event` | no | `rules_proposal` (when its actions change), `decision`, `decision_changed`, `decision_skipped` |

**Rule fallback** (`config/rules.yaml`): action templates by target type, resolved to devices from the city description: flood -> open the valves controlling the zone (`opening` 1.0) + signal priority in the zone (+ rescue unit from `high`); fire -> nearest free fire truck + signal priority (+ ambulance from `high`); accident -> nearest free ambulance + signal priority (+ police from `high`); traffic jam -> signal priority; power problem -> shed the grid switch controlling the zone. Never a quarantined device, a busy unit or a target that already failed for the incident; what cannot be resolved is left out and written in the justification.

**Score** (`config/scoring.yaml`): `P = confidence x severity_weight x zone_importance x (1 + waiting_s / 60)`; per action `P x effectiveness - cost - risk x (1 - trust)` (minus `2.5 x risk x P` for a risky action in safe mode unless 2 sensor types agree); a proposal scores the sum of its actions with a positive score, actions <= 0 are dropped. Worked example: `python -m tests.scoring.worked_example`.

**Design choices to know:**

- **When to decide.** Waits for the LLM proposal up to `llm.yaml budget.answer_s` (8 s, override `decide.budget_s`). Decides on the rules at once if `llm-status` is offline or the LLM gave up on that incident. `critical`: decides at once, then switches if a better LLM proposal arrives within the budget. Updates only trigger a new decision on a severity increase or a new evidence type. Tie: the rule proposal.
- **Checked proposals.** Unknown target, action not allowed (meta `actions`, `rules.yaml unit_actions` for units), quarantined, or failed before: dropped and named in the explanation.
- **Units.** Nearest free unit (zone hops on the meta adjacency); a free unit wanted by an undecided incident of higher priority is left to it (B5: the numbers are in the explanation). A unit held by another incident is taken only if `P >= 1.5 x` the holder's P: the Twin refuses a dispatch to a busy unit, so the decision sends a `recall`, and the incident is decided again with the dispatch once the Twin reports the unit `free`. Incidents that could not get a unit get one the same way when one becomes free.
- **Later decisions list only new actions** (never resent, so the output's cooldown never refuses them); nothing is undone. After `failed` / `refused` the target is excluded for the incident (at most `max_redecisions`).
- **No decision** (`decision_skipped`) when all evidence has trust 0 (Guardian's latest verdict wins over detection's trust), or the best score is <= 0.
- **Closed incident:** forgotten; its units are not recalled (out of scope). Every decision is logged with its full score breakdown in `logs/scoring.jsonl`.
## Output module

The last gate. Every action of a `brain/internal/decision` goes through the safety rules; what passes becomes a signed command on `brain/cmd/{target_id}`, followed until the Twin acknowledges it and the city reacts.

```
decision ─▶ S1..S6 ─refused─▶ outcome refused + brain/event action_refused (red on the timeline)
              │ pass
              ▼
   BrainCommand (brain-000042, correlation_id of the chain), sealed for Twin + Guardian ─▶ brain/cmd/{target} QoS 1
              │ outcome sent + command_sent
   ack done ──┼─▶ acknowledged ─▶ effect check (readings move within 30 s?) ─no─▶ failed no_effect + alert
   rejected ──┼─▶ failed (reason) + alert command_rejected
   nothing 4 s┴─▶ resent once, same id ─nothing 4 s─▶ failed no_ack + alert actuator_not_responding
```

**Safety rules** (`config/safety.yaml`), in this order; the first that fails refuses the action, with its rule and a reason:

| # | Refused when |
|---|---|
| S1 | target quarantined by Guardian (`brain/internal/quarantine/+`) |
| S2 | every piece of evidence of the incident is rejected (trust 0, or Guardian's current verdict `rejected`) |
| S3 | risky action (`risky_actions`: valve `open`, power grid `shed`, unit `dispatch`) with fewer than 2 different sources agreeing |
| S4 | same action on the same device sent less than 60 s ago (`s4_scope: any_action` widens it to any action) |
| S5 | action not allowed for the device (actuator `actions`; units `dispatch` / `recall`), or target unknown / no city description yet |
| S6 | risky action while Guardian is offline (layer status, or `mode: safe` in the decision), unless 2 different **sensor types** agree |

**Counting sources (S3, S6).** Evidence is the incident's evidence list (`brain/internal/incident`) plus the decision's `evidence_sensors`, with Guardian's current verdict applied: a reading counts when its trust is above 0. Each sensor is one source. All the emergency calls of an incident count together as **one** source: detection groups calls by zone, so ten calls from one zone (B7) never replace a second sensor. S6 counts sensor types only; calls never count there.

**Published:** `brain/cmd/{target_id}` (sealed, `hmacs` twin + guardian, QoS 1), `brain/internal/outcome` (`sent`; `refused` + `rule` + `detail`; `acknowledged`; `failed` + `detail` = the Twin's reason, `no_ack` or `no_effect`), `brain/event` (`command_sent`, `action_refused`, `command_acknowledged`, `command_retried`, `command_failed`, `effect_confirmed`, `safe_mode_on`, `safe_mode_off`), `brain/alert` (`actuator_not_responding`, `actuator_no_effect`, `command_rejected`).
**Consumes:** `brain/internal/decision`, `incident`, `ack`, `reading`, `meta`, `quarantine/+`, `trust/+`, `layer-status/+`.

**Design choices to know:**

- **Never blocks.** Acks, retries and effect checks are timers checked by `tick()` (every 0.5 s); nothing waits.
- **Retry = same bytes.** The retry republishes the exact signed payload (same id, same timestamp). The Twin answers a known id with its first ack and never applies it twice.
- **Idempotent.** A decision id is executed once (remembered 1 h); a duplicate or late ack is ignored and logged.
- **Dispatch zone.** A unit `dispatch` always carries `zone_id` = the incident zone (set if missing, replaced and logged if different).
- **Command ids** `brain-NNNNNN` start at the tenths of a second since UTC midnight (`commands.id_start: auto`), so a restarted module never reuses an id of the same day.
- **Effect check** (`effects` in `config/output.yaml`): valve `open` → water level falls, grid `shed` → power load falls, signal `priority` → traffic speed rises, fire truck `dispatch` → air quality falls. "Falls" = one reading at least the margin below the highest value since the command was sent (margin set above sensor noise). A projected-trend test was rejected: a flood levelling off on its own would let a stuck valve pass. Actions without an entry, or without readings, are not checked.
- **Tolerant reader.** Unknown fields in a decision are dropped and logged, so an additive change in the scoring module never stops a command.

Tests: `tests/output/`: each rule refuses its case and passes the valid one; sealing; acks, retry, alert; effect check; idempotency; and a loop through the real Twin mock and input module (valve acknowledged with effect, `stuck` valve → `no_effect`, `no_ack` → retry → alert, busy unit → `unit_busy`).

---

## Timeline module

The demo screen (R3) and the run logs (R2). Run `python -m src.timeline`, then open `http://127.0.0.1:8090` in Chrome or Edge (F11, or F, for full screen). It is one self-contained page: no internet, no external file.

**What it shows**, from its own MQTT client (`subscribe` in `config/timeline.yaml`):

| Layer | Topics | Rows |
|---|---|---|
| TWIN | `city/+/call`, `twin/actuator/+`, `twin/unit/+`, `twin/status`, `brain/internal/reading` | calls, acks (linked to their command), unit movements, readings crossing their alert threshold, evidence readings of an incident |
| GUARDIAN | `guardian/verdict/+`, `guardian/quarantine/+`, `guardian/status`, `brain/internal/layer-status/+` | non-trusted verdicts, verdict changes, the verdict of an incident's evidence sensors, quarantines, offline = safe mode |
| BRAIN | `brain/event`, `brain/error`, `brain/alert`, `brain/cmd/+`, `brain/status`, `brain/internal/incident`, `decision`, `proposal` | incidents, decisions with scores, commands, refusals, rejected messages, alerts |
| LLM | `brain/internal/llm-status`, `brain/event` `llm_*` | proposals, timeouts, online / offline |

Raw readings are not rows (200 msg/s): they feed the latest values and sparklines of the city map, sampled once a second per zone.

**Page layout.** Header: layer pills (TWIN, GUARDIAN, BRAIN, LLM: online / offline / degraded), a blinking SAFE MODE badge, the running scenario with its elapsed time and log file, a clock. Counter strip: messages/s, commands sent / acknowledged / refused / failed, alerts, rejected input, open incidents. Left (60 %): the live timeline, one row per event `HH:MM:SS.mmm  LAYER  text`, colour per layer; refusals, errors and alerts in red, safe mode and retries in amber, acks and confirmed effects in green; a lock mark when the HMAC was verified, a broken lock when it was wrong or missing. Rows of one chain (`correlation_id`) share the colour of their left bar. Hovering a row pauses the scroll and highlights its whole chain; clicking opens the chain (rows on screen, plus the graph's `GET /chain/{cid}` when it answers). Right: the city map, the open incidents (type, zone, confidence bar, severity, verified) and the last decision (actions, chosen proposal against the alternatives as score bars, explanation).

**City map** (press **M** or the VIEW button: map focus, where the map takes the left of the screen and the timeline the right; remembered per screen). It is drawn from `twin/meta`, so another Twin's city is drawn without code changes. `config/timeline.yaml` → `map.layout` places the demo city's zones on a grid (`sea: west` draws the coast), and unlisted zones fill the free cells. The Brain sits in the free cell nearest the centre. It shows the city **as the Brain believes it**:
- **Water and smoke** rise inside a zone with its water-level and air readings, relative to the sensor's alert threshold. A reading Guardian rejected, or from a quarantined sensor, does not raise them (T5: the forged 1.8 m never floods the Port, and the value is marked *not believed*).
- **Roads** link adjacent zones, coloured by traffic density (green, amber, red). A signal in `priority` lays an animated green corridor.
- **Sensors** are dots coloured by Guardian's verdict (grey when Guardian is offline: nothing is being checked). A quarantined device gets a red mark.
- **Actuators** sit on the zone's bottom edge with their live state (valve OPEN, signal PRIO, grid SHED, which dims the zones it feeds). A command refused by a safety rule shows *REFUSED S6* above the device.
- **Units** (A ambulance, F fire truck, P police, R rescue) drive between zones as the Twin reports `en_route`, `on_scene` and `returning`.
- **Commands** are beams from the Brain to the target: blue when sent, green when acknowledged, red when rejected or failed.
- **Emergency calls** ring in their zone with a count. **Safe mode** turns the map frame amber.

**Endpoints:** `/` page, `/events` (Server-Sent Events, catch-up with `Last-Event-ID`), `/api/recent`, `/api/status`, `/api/chain/{cid}`.

**Run logs (R2).** A `brain/control/run` message `{run_id, scenario_id, action: start|stop}` (plain or sealed) opens `logs/<scenario_id>-<YYYYMMDD-HHMMSS>.log` in the repository-root `logs/` folder (`logs.dir: ../logs`) and closes it on `stop`. Outside runs, lines go to `logs/live-<YYYYMMDD>.log`. JSON lines: every row, with the full body of each `brain/event`, `brain/error`, alert and run message, and `shown: false` for rows logged but not displayed. Each line is flushed at once. The start message is retained: a restarted timeline continues the same file (name from the run's own timestamp, append mode), so one run gives one file.

**Rehearse without a broker:** `python -m src.timeline.demo` serves a scripted flood chain on port 8091.

Tests: `tests/timeline/`: every topic type with sealed envelopes, bad payloads, signature marks, log rotation on start / stop / restart, the page, `/api/*` and the SSE stream on an ephemeral port.
