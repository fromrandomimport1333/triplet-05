# City Brain: Team Overview and Shared Contracts

**Read this first.** Every team needs this document. Each team then has its own document:

| #   | Team                                      | People | Document                               |
| --- | ----------------------------------------- | ------ | -------------------------------------- |
| 1   | **Input**                                 | 1      | [`01-input.md`](01-input.md)           |
| 2   | **Graph** (detection + graph DB)          | 2      | [`02-graph.md`](02-graph.md)           |
| 3   | **LLM**                                   | 1      | [`03-llm.md`](03-llm.md)               |
| 4   | **Math model** (scoring + rule fallback)  | 1      | [`04-math-model.md`](04-math-model.md) |
| 5   | **Output** (safety + commands + timeline) | 2      | [`05-output.md`](05-output.md)         |
| 6   | **Mock**                                  | 1      | [`06-mock.md`](06-mock.md)             |

**Reference documents:**

- [`../city-brain-interface-spec.pdf`](../city-brain-interface-spec.pdf): what we exchange with the Twin and Guardian layers (topics, envelope, HMAC, error codes).
- [`../city-brain-inputs-outputs.md`](../city-brain-inputs-outputs.md): architecture, inputs, outputs.
- [`../guardian-proposal-alignment.md`](../guardian-proposal-alignment.md): open points with the Guardian team (mTLS, Ed25519, trust score). **These may change some details below**; the input team follows them.

---

## 1. Architecture and who owns what

<img src="../city-brain-architecture.png" title="" alt="City Brain architecture" width="748" />

```
 Twin / Guardian (or mocks)                                                  Twin actuators (or mocks)
          │ MQTT + TLS                                                                 ▲ MQTT + TLS
          ▼                                                                            │
 ┌─────────────┐   readings, calls,   ┌──────────────┐  incidents  ┌────────┐ proposals ┌────────────┐ decisions ┌─────────────────┐
 │ 1. INPUT    │──trust, quarantine──▶│ 2. GRAPH     │────────────▶│ 3. LLM │──────────▶│ 4. MATH    │──────────▶│ 5. OUTPUT       │
 │ check +     │                      │ detection +  │             └────────┘           │ scoring +  │           │ safety rules,   │
 │ validate    │                      │ graph DB     │──incidents──────────────────────▶│ rule       │           │ sign, commands, │
 └─────────────┘                      └──────────────┘  ▲ context  (to rule fallback)   │ fallback   │           │ timeline, logs  │
                                             ▲          └─ LLM asks graph for context   └────────────┘           └─────────────────┘
                                             └───────────────────── all events stored and linked ◀──────────────────────┘
                                                                                                    6. MOCK: mock Twin, Guardian, actuators, scenarios
```

| Block                                                                                   | Team                 |
| --------------------------------------------------------------------------------------- | -------------------- |
| ① Check incoming messages (TLS, HMAC, timestamp, id, format, IDs, ranges)               | **Input**            |
| Detection stage (sensor streams → incidents)                                            | **Graph**, person A  |
| City graph DB (city map, linked events, context queries)                                | **Graph**, person B  |
| Local LLM on the edge device (proposals)                                                | **LLM**              |
| Scoring model with Guardian trust                                                       | **Math model**       |
| Rule fallback (standard action per incident)                                            | **Math model**       |
| Safety rules, ② sign outgoing, commands, acknowledgements                               | **Output**, person A |
| Timeline, logs, `brain/event`                                                           | **Output**, person B |
| Mock Twin, mock Guardian, mock actuators, scenario runner                               | **Mock**             |
| Coordination with the Twin and Guardian teams, repo, Docker, Test Book, report assembly | **Team lead**        |

---

## 2. How the teams talk to each other: internal topics

All internal modules talk through **MQTT topics under `brain/internal/`** on the same broker. This lets each team build and test its module **alone**, by publishing fake messages on the topics it consumes.

- Only Brain accounts can publish or subscribe on `brain/internal/#` (broker ACL).
- Internal messages travel over TLS but **don't need an HMAC**: the input team has already checked everything coming from outside.
- All internal messages are plain JSON with the common fields below.

| Internal topic                | Published by              | Consumed by                 | Content                                             | Retained                                                 |
| ----------------------------- | ------------------------- | --------------------------- | --------------------------------------------------- | -------------------------------------------------------- |
| `brain/internal/meta`         | Input                     | Graph, Math, Output, LLM    | City description (zones, sensors, actuators, units) | yes                                                      |
| `brain/internal/reading`      | Input                     | Graph (detection)           | Validated sensor reading                            | no                                                       |
| `brain/internal/call`         | Input                     | Graph (detection), LLM      | Validated emergency call                            | no                                                       |
| `brain/internal/trust`        | Input                     | Graph (detection), Math     | Guardian verdict per sensor                         | yes, per sensor: `brain/internal/trust/{sensor_id}`      |
| `brain/internal/quarantine`   | Input                     | Math, Output                | Quarantined / released devices                      | yes, per device: `brain/internal/quarantine/{device_id}` |
| `brain/internal/ack`          | Input                     | Output, Graph               | Twin acknowledgements and actuator / unit state     | no                                                       |
| `brain/internal/layer-status` | Input                     | Math, Output                | Twin / Guardian online or offline                   | yes, per layer                                           |
| `brain/internal/incident`     | Graph (detection)         | LLM, Math, Graph DB, Output | Incident opened / updated / closed                  | no                                                       |
| `brain/internal/proposal`     | LLM, Math (rule fallback) | Math (scoring)              | Proposed actions for an incident                    | no                                                       |
| `brain/internal/decision`     | Math                      | Output                      | Chosen actions, with scores                         | no                                                       |
| `brain/internal/outcome`      | Output                    | Graph, Math                 | Action sent / refused / acknowledged / failed       | no                                                       |
| `brain/event`                 | Everyone                  | Timeline (Output B)         | Timeline events (external topic, spec BR-3)         | no                                                       |
| `brain/error`                 | Input                     | Timeline                    | Rejected external messages with error code          | no                                                       |

**Graph context service:** besides topics, the graph team exposes a small **HTTP API on the Brain host** for questions that need an answer (see `02-graph.md`): `GET /context/{incident_id}`, `GET /zone/{zone_id}`, `GET /actuators?zone_id=...`.

---

## 3. Shared data formats

Every internal message has these common fields:

| Field            | Meaning                                                                                                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id`             | Unique id, prefixed by the module: `in-…`, `det-…`, `llm-…`, `math-…`, `out-…`                                                                                           |
| `timestamp`      | ISO 8601 UTC with milliseconds, e.g. `2026-11-14T10:22:03.120Z`                                                                                                          |
| `correlation_id` | Id of the **original external message** (reading or call) that started the chain. Copied unchanged through every step, so the graph and the timeline can link everything |

### Reading (`brain/internal/reading`)

```json
{"id": "in-000501", "timestamp": "2026-11-14T10:22:03.150Z", "correlation_id": "twin-000182",
 "sensor_id": "water-level-04", "zone_id": "port", "type": "water_level",
 "value": 2.4, "unit": "m", "origin": "real"}
```

`origin` is `real` or `simulated` if the Twin provides it, otherwise absent.

### Call (`brain/internal/call`)

```json
{"id": "in-000502", "timestamp": "2026-11-14T10:22:05.010Z", "correlation_id": "twin-000190",
 "call_id": "call-0012", "zone_id": "z3", "category": "fire", "text": "Smoke coming from the substation"}
```

### Trust (`brain/internal/trust/{sensor_id}`)

```json
{"id": "in-000503", "timestamp": "2026-11-14T10:22:03.910Z", "correlation_id": "twin-000182",
 "sensor_id": "water-level-04", "verdict": "rejected", "trust_score": 0.12, "reason": "out_of_range"}
```

`verdict` is `trusted`, `suspicious` or `rejected` (Guardian's naming). `trust_score` (0–1) if Guardian provides it, otherwise `null`.

### Quarantine (`brain/internal/quarantine/{device_id}`)

```json
{"id": "in-000510", "timestamp": "2026-11-14T10:23:10.020Z", "device_id": "valve-port-main",
 "state": "quarantined", "reason": "inconsistent"}
```

### Layer status (`brain/internal/layer-status/{layer}`)

```json
{"id": "in-000511", "timestamp": "2026-11-14T10:30:00.000Z", "layer": "guardian", "status": "offline"}
```

### Incident (`brain/internal/incident`)

```json
{"id": "det-000031", "timestamp": "2026-11-14T10:22:03.200Z", "correlation_id": "twin-000182",
 "incident_id": "inc-0031", "type": "flood", "zone_id": "port",
 "status": "opened", "confidence": 0.76, "severity": "medium", "verified": true,
 "evidence": [
   {"source_id": "water-level-04", "kind": "reading", "value": 2.4, "unit": "m", "trust": 1.0},
   {"source_id": "call-0011", "kind": "call", "category": "flood", "trust": 1.0}
 ]}
```

- `type`: `flood`, `fire`, `accident`, `traffic_jam`, `power_problem`.
- `status`: `opened`, `updated`, `closed`.
- `severity`: `low`, `medium`, `high`, `critical`.
- `verified`: `false` when Guardian was offline while the incident was detected.

### Proposal (`brain/internal/proposal`)

```json
{"id": "llm-000012", "timestamp": "2026-11-14T10:22:05.900Z", "correlation_id": "twin-000182",
 "incident_id": "inc-0031", "origin": "llm",
 "actions": [
   {"target_id": "valve-port-up", "action": "open", "params": {"percent": 80}},
   {"target_id": "signal-port-2", "action": "close_inbound", "params": {}}
 ],
 "justification": "Water rising fast at the port; upstream valve reduces inflow."}
```

`origin` is `llm` or `rules`. `target_id` is an actuator or unit id from the city description.

### Decision (`brain/internal/decision`)

```json
{"id": "math-000020", "timestamp": "2026-11-14T10:22:05.950Z", "correlation_id": "twin-000182",
 "incident_id": "inc-0031", "chosen_proposal": "llm-000012",
 "actions": [{"target_id": "valve-port-up", "action": "open", "params": {"percent": 80}}],
 "score": 1.53,
 "alternatives": [{"proposal_id": "rules-000009", "score": 1.21}],
 "risk": "high", "evidence_sensors": ["water-level-04", "water-level-05"]}
```

### Outcome (`brain/internal/outcome`)

```json
{"id": "out-000044", "timestamp": "2026-11-14T10:22:06.400Z", "correlation_id": "twin-000182",
 "incident_id": "inc-0031", "decision_id": "math-000020", "target_id": "valve-port-up",
 "status": "acknowledged", "command_id": "brain-000042", "detail": "state open_80"}
```

`status`: `sent`, `refused` (blocked by a safety rule, `detail` says which), `acknowledged`, `failed` (no ack or Twin rejected it).

### Timeline event (`brain/event`, external)

```json
{"id": "out-000045", "timestamp": "2026-11-14T10:22:06.410Z", "source": "brain", "correlation_id": "twin-000182",
 "event": "command_acknowledged", "incident_id": "inc-0031",
 "summary": "valve-port-up opened 80% (flood at port)"}
```

**JSON Schemas** for all formats go in `brain/contracts/schemas/`. The team that publishes a message owns its schema. Any change is announced to the teams that consume it **before** it's merged.

---

## 4. Repository layout

```
brain/
├── README.md                 # how to run the Brain alone with mocks
├── INTERFACE.md              # Interface Card (R1), from the interface spec
├── docker-compose.yml        # broker + all Brain modules + mocks
├── config/                   # broker address, topics, keys paths, thresholds (never hard-coded)
├── contracts/schemas/        # JSON Schemas of every internal and external message
├── src/
│   ├── common/               # shared: MQTT client, config loader, logging, models (pydantic)
│   ├── input/                # team 1
│   ├── detection/            # team 2, person A
│   ├── graph/                # team 2, person B
│   ├── llm/                  # team 3 (runs on the edge device)
│   ├── scoring/              # team 4
│   ├── rules/                # team 4
│   ├── output/               # team 5, person A (safety, signer, commands)
│   └── timeline/             # team 5, person B
├── mocks/                    # team 6 (+ mock LLM by team 3)
├── scenarios/                # one file per Test Book scenario + run script
├── logs/                     # one log file per scenario run (R2)
└── tests/
```

## 5. Common rules for everyone

| Rule                   | Detail                                                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Language               | Python 3.11+ for every module                                                                                                   |
| Messages               | pydantic models in `src/common/models.py`, generated from / checked against the JSON Schemas                                    |
| Config                 | Everything configurable in `config/*.yaml`: broker, topics, thresholds, timeouts. **Nothing hard-coded** (the jury checks this) |
| Logs                   | JSON lines, one per event, with `correlation_id`, written to `logs/`                                                            |
| Each module runs alone | `python -m src.<module>` with only the broker and mocks running                                                                 |
| Tests                  | Each team writes unit tests for its module in `tests/<module>/`                                                                 |
| Git                    | One branch per task, pull request, **one reviewer from another team**, merge when tests pass                                    |
| Secrets                | Keys and certificates in `secrets/` (git-ignored), never in code or chat                                                        |
| No names               | No team or member names in code, docs, report or slides                                                                         |

## 6. Shared timeline

| Week  | Dates          | Goal for **all** teams                                                                                                                                                           |
| ----- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1** | 15 – 19 Oct    | **Fri 17 Oct: internal formats frozen** (section 3). Each module skeleton connects to the broker, consumes and publishes its topics with fake data. Mock Twin publishes readings |
| **2** | 20 – 26 Oct    | Each module does its real job. **Sun 26 Oct: first end-to-end run on mocks**: fake flood reading → incident → proposal → decision → command → ack → timeline                     |
| **3** | 27 Oct – 2 Nov | **Joint run with the real Twin and Guardian** (target Wed 29 Oct). All Brain scenarios passing. Bug fixing, tuning                                                               |
| **4** | 3 – 7 Nov      | Freeze code (Tue 4 Nov). Report sections (Wed 5), review, **submit Fri 7 Nov**                                                                                                   |
| —     | 8 – 14 Nov     | Rehearse the demo twice on our own router; CSTAM on 14 Nov                                                                                                                       |

**Meetings:** 10-minute stand-up every two days (done / doing / blocked) + one 45-minute team sync per week + one weekly triplet sync (team lead).

## 7. Definition of done (for every task)

- [ ] Code merged, tests pass
- [ ] Works with the mocks, started by one command
- [ ] Consumes and publishes exactly the formats of section 3
- [ ] Every important step appears on the timeline (`brain/event`)
- [ ] Settings in `config/`, not in code
- [ ] A short paragraph written for the report (what, how, why, limits)
