# Team 2: Graph (detection + graph DB)

**People:** 2 · **Code:** `brain/src/detection/` (person A), `brain/src/graph/` (person B) · **Read first:** [`00-overview-and-contracts.md`](00-overview-and-contracts.md)

## 1. Mission

You turn a **stream of numbers into incidents**, and you keep the Brain's **memory**: the city map and every event, linked together, so the Brain can see what is connected to what.

| Person | Role |
|---|---|
| **A: Detection** | Turns sensor readings and calls into incidents (open / update / close) |
| **B: Graph DB** | Stores the city map and every event, links them, answers context questions |

You work very closely: detection uses the graph to know which zones are neighbours, and the graph stores every incident detection produces.

## 2. You own / you don't own

| You own | You don't own |
|---|---|
| Detection stage: per-sensor abnormality, combining signals, incident lifecycle | Checking signatures and formats (Input) |
| Applying Guardian's trust in detection | Deciding the response (LLM, Math) |
| Graph DB: schema, loading the city map, storing all events | The timeline screen (Output B) |
| Context API for the LLM and the Math team | |

## 3. Your inputs and outputs

| Direction | Topic / API | Content |
|---|---|---|
| In | `brain/internal/meta` | City description → graph nodes (zones, sensors, actuators, units) |
| In | `brain/internal/reading`, `call` | Readings and calls → detection + stored in graph |
| In | `brain/internal/trust/#` | Guardian verdicts → detection weights + stored |
| In | `brain/internal/decision`, `outcome`, `ack` | Decisions and actions → stored and linked |
| Out | `brain/internal/incident` | Incidents opened / updated / closed |
| Out | `brain/event` | `incident_opened`, `incident_updated`, `incident_closed` for the timeline |
| Out | HTTP API (person B) | Context for the LLM and Math (section 5) |

## 4. Person A: detection stage

### 4.1 Step 1: is this sensor abnormal?
For each sensor keep a small memory of its normal behaviour:
- running average and usual variation (exponential moving average);
- abnormal when **several readings in a row** are far from normal (one spike = noise), **or** the value is above the sensor's `alert` level from the city description.

Start values (in `config/detection.yaml`): smoothing 0.05, "far" = 3× usual variation, "several" = 3 readings.

### 4.2 Step 2: combine signals per zone
| Incident | Signals (same zone or neighbouring zones from the graph) |
|---|---|
| `flood` | Water level abnormal **and** rising · optional flood call |
| `fire` | Air quality abnormal **and/or** fire call · optional power spike |
| `accident` | Accident call **and/or** sudden traffic speed drop |
| `traffic_jam` | Traffic speed low for several minutes, outside the normal rush-hour pattern |
| `power_problem` | Power load abnormal with no matching fire |

More agreeing signals → higher **confidence** (0 to 1). Write the combination rule so the math team can explain it in the report.

### 4.3 Step 3: Guardian's trust
| Verdict | Weight of that reading |
|---|---|
| trusted | `trust_score` if given, else 1.0 |
| suspicious | `trust_score` if given, else 0.3 |
| rejected | 0 (ignored) |
| no verdict / Guardian offline | 0.6, and the incident is marked `verified: false` |

### 4.4 Step 4: lifecycle
- **Open** when confidence ≥ 0.5 on two checks in a row
- **Update** on new evidence or a severity change
- **Close** when confidence < 0.25
- **One open incident per zone + type** (no duplicates)

Publish an incident message only on **open, update, close**, never on every reading. The LLM is called on each of them.

## 5. Person B: graph DB

### 5.1 Nodes and links
| Nodes | Links |
|---|---|
| Zone, Sensor, Actuator, Unit (from `meta`) | zone `NEXT_TO` zone · sensor `IN` zone · actuator `CONTROLS` zone · unit `BASED_IN` zone |
| Reading (abnormal or linked to an incident only), Call, Verdict, Incident, Decision, Action, Ack | reading `FROM` sensor · verdict `ON` reading · incident `IN` zone · incident `EVIDENCE` reading/call · decision `FOR` incident · action `EXECUTES` decision · ack `CONFIRMS` action |

Use `correlation_id` to link events automatically.

**Don't store every normal reading.** Store readings that are abnormal, rejected, or used as evidence; keep only the latest value per sensor otherwise. This keeps the graph fast on small hardware.

### 5.2 Context API (HTTP, on the Brain host)
| Endpoint | Returns | Used by |
|---|---|---|
| `GET /context/{incident_id}` | The incident, its evidence, related events in the same and neighbouring zones in the last 15 min, available actuators and units, quarantined devices | LLM |
| `GET /zone/{zone_id}` | Neighbours, sensors with latest value and trust, actuators that control it, units in it | LLM, Math |
| `GET /actuators?zone_id=…` | Actuators that control the zone, with state and quarantine status | Math, Output |
| `GET /chain/{correlation_id}` | Full chain reading → verdict → incident → decision → action → ack | Timeline, report |

Answer in < 100 ms. Return compact JSON (the LLM's prompt must stay short).

### 5.3 Technology: decide by Fri 17 Oct
| Option | Pros | Cons |
|---|---|---|
| **Neo4j Community** | Best known, Cypher queries, built-in browser: great to show the graph to the jury | Java, ~1 GB RAM; run it on the Brain host, not the edge device |
| **Memgraph** | Same query language, in memory, lighter | Less known |
| **NetworkX** | Simplest, no server | Not a real database, no query language |

## 6. Tasks

### Week 1 (15–19 Oct)
- [ ] **B:** choose the technology, run it in Docker, load the city description from `meta`
- [ ] **A:** consume readings from the mock, compute per-sensor normal behaviour, log "abnormal"
- [ ] **Both:** incident format frozen with LLM, Math and Output teams (**Fri 17 Oct**)

### Week 2 (20–26 Oct)
- [ ] **A:** combination rules for the 5 incident types, trust weights, lifecycle; publish incidents
- [ ] **B:** store events and links; `/context` and `/zone` endpoints
- [ ] **Sun 26 Oct:** end-to-end run on mocks

### Week 3 (27 Oct – 2 Nov)
- [ ] **A:** tune thresholds with the real sensors (real data is noisier); rush-hour pattern for traffic
- [ ] **B:** `/actuators`, `/chain`; graph view for the demo (screenshot or live browser)
- [ ] Joint run

### Week 4 (3–7 Nov)
- [ ] Report: detection method, graph schema, one example chain

## 7. Done when

- [ ] **Calm day:** no incident · **rush hour:** no false jam
- [ ] **Real flood:** one incident opened within 3 readings, updated, then closed
- [ ] **Fake flood** (Guardian rejects the sensor): no incident
- [ ] **Guardian offline:** incident still detected, marked `verified: false`
- [ ] No duplicate incidents for a long flood
- [ ] `/context/{incident_id}` answers in < 100 ms with < 1,500 characters of JSON
- [ ] `/chain/{correlation_id}` returns the full chain for scenario B2

## 8. Who you work with

| Team | About |
|---|---|
| **Input** | Format of readings, calls, trust |
| **LLM** | What the context must contain, and how short it must be |
| **Math** | Incident fields they score on (confidence, severity, evidence) |
| **Output B** | Incident events for the timeline; the `/chain` endpoint |
| **Mock** | Scenarios with realistic noise, rush hour, faulty sensors |

## 9. Risks and tips

- Real sensors are noisy and sometimes silent: a sensor with no reading for a while must not keep an incident open forever.
- Keep every threshold in `config/detection.yaml`; you will tune them a lot.
- Graph growth: test with a 30-minute scenario before the demo.
