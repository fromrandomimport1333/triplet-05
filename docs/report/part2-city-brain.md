# Part 2: The City Brain

## 2.1 Role and goals

The City Brain answers *"What should we do?"* It detects real incidents (floods, fires, accidents,
traffic jams, power problems) in the Twin's live data, decides the response, and acts through the
Twin's actuators and response units, while the Guardian tells it which data to believe. Four goals
shaped every choice:

1. **Correct under falsified data.** A fake reading, a forged message or an injected call must never
   move a device (organizer example T5: *"does not open the valve, and records why"*).
2. **Never stuck.** Every incident gets a decision, even when the local LLM, the Guardian or the
   graph is down; every refusal says why.
3. **Visible.** Every step, from reading to acknowledgement, is on the live timeline and in the run
   log, linked by the id of the message that started the chain (R2, R3).
4. **Connectable.** Nothing about the partners is in the code: topics, field names, keys and the
   broker are configuration; the city itself is learned from `twin/meta` (R1).

## 2.2 Architecture

Nine Python modules, each a separate process, talk over MQTT topics `brain/internal/*`. Each
module is a pure service (messages in, messages out, injected clock), tested without a broker; only
a thin entry point wires it to MQTT. `python run.py` starts them all and restarts a crashed one.

```
 Twin ──readings, calls, acks──┐                                         ┌──> brain/cmd/{device} ──> Twin
 Guardian ──verdicts, quarantine┤                                         │     (signed, QoS 1)
                                ▼                                         │
   ┌────────┐  reading/call/trust  ┌───────────┐ incident ┌─────┐ proposal ┌─────────┐ decision ┌────────┐
   │ INPUT  │ ───────────────────▶ │ DETECTION │ ───────▶ │ LLM │ ───────▶ │ SCORING │ ───────▶ │ OUTPUT │
   │7 checks│  meta, quarantine,   └───────────┘     │    └─────┘   ▲      │+ RULES  │          │S1 – S7 │
   └────────┘  layer status (retained)   │           └──────────────┼────▶└─────────┘ outcome  └────────┘
        │                                 ▼                  context│                ◀─────────────┘
   brain/error                       ┌─────────┐ GET /context ──────┘        ┌──────────────────────┐
                                     │  GRAPH  │ ◀── every message ─────────▶│ TIMELINE (web :8090) │
                                     └─────────┘     linked by correlation_id│ + one log per run    │
                                                                             └──────────────────────┘
```

**Front door (input).** Every Twin and Guardian message passes seven checks in order: envelope,
HMAC-SHA256 signature, timestamp within ±10 s, replay (id and `seq`), format, ids known from
`twin/meta`, value within the sensor's range. A refused message goes to `brain/error` with its code
(`E_AUTH`, `E_STALE`…); accepted ones become clean internal messages. City description, verdicts,
quarantines and layer status are retained, so a restarted module recovers the full state at once.

**Incident detection.** Each sensor keeps an exponentially weighted mean and variance of its normal
behaviour (learned only from quiet readings, frozen while abnormal). A sensor is abnormal after
3 readings in a row more than 3 standard deviations away in the bad direction, or at its alert
level. Signals are then combined per zone and incident type: a flood needs the water level abnormal
and rising (or a flood call), a fire air quality and/or fire calls, an accident a call and/or a
sudden speed drop. Each source gets a probability `p = min(cap, weight × strength × trust)` and the
confidence is `1 − ∏(1 − p)`. Caps make one source alone weak: a single reading reaches at most
0.6, and all the calls of one zone count as **one** source capped at 0.55, so ten fake calls are
still one source (B7). How long a call counts was measured, not guessed: in 5,613 NYC 311 flood calls
during Hurricane Ida (our dataset study), calls from one ~1 km area came a median 19 minutes apart
on the storm night, and half of the flooded areas got a second call within an hour of the first. So a
flood call counts for an hour and two callers give full strength (`tools/calibrate_calls.py`
recomputes this). Fire and accident calls keep a 2-minute window, because 311 is not an emergency
line. **Guardian's trust** enters here: trusted readings weigh 1 (or the
Guardian's score), suspicious 0.3, rejected 0 (ignored, so a rejected fake flood opens nothing:
B3/T5), and readings without a verdict 0.6 with the incident marked *not verified*. An incident
opens at confidence ≥ 0.5 on two checks in a row, is updated only on new evidence or a severity
change, and closes below 0.25, so a long flood is one incident. A rush hour is recognised as
city-wide (most zones slow together) and is not a jam (B1).

**Case memory: fire or burnt toast?** Smoke alone does not say whether something is burning. The
case memory compares the last 60 s of a zone's smoke, CO, CO₂ and ceiling temperature (level above
the sensor's own baseline, and trend) with a library of 4,998 windows of the NIST smoke alarm tests
(90 lab tests: flaming and smouldering fires, and four kinds of cooking). The 15 most similar lab
cases vote: fire, cooking or quiet. The library and features come from our dataset study
(`brain/vector-storage/`), where this search recognised the held-out test group 98.7 % of the time
(our standard-library port gives the same 98.7 %). The physical reason is CO: it rises in all 30
fire tests and in none of the 60 cooking tests. In the Brain the search is a plain in-memory scan of
about 5,000 points (a few milliseconds, offline, no vector database). Its verdict is **not
evidence**: it comes from the same sensors, so the safety rules never count it as a second source.
A *fire* verdict is a support signal for the fire incident. A *cooking* verdict holds the smoke
readings down (× 0.2), while residents' calls still count in full. Smoke alone, with no call, in a
zone with these sensors waits up to 25 s for the verdict before a fire opens. The memory never talks
a fire down, and a verdict needs 60 % of the votes. In B14 the Twin replays a burnt toast test held
out of the library: as much smoke as the B5 fire, but no fire opens and no fire truck leaves. In B5
and B11 the replayed fire is recognised as a fire. A Twin without CO sensors gets the plain
behaviour.

**Decision method: rules, LLM and a score.** For every incident, two proposals compete.

- *Rule fallback* (`config/rules.yaml`): the standard response per incident type, resolved against
  the city: a flood opens the valve controlling the zone and gives traffic priority; a fire sends the
  nearest free fire truck. It is ready at once and guarantees a decision.
- *Local LLM* (Ollama, `qwen2.5-coder:7b`, on the demo laptop's GPU, offline): it receives a compact
  context (under 1,500 characters: the incident, evidence, devices and units that can act, quarantined
  devices, other open incidents nearby, recent calls) and answers JSON constrained by a schema that
  lists only this incident's devices and actions. It can link signals rules do not (a fire, a power
  spike and a jam next door: B11) and read call text.
- *Scoring* chooses: `P = confidence × severity × zone importance × (1 + waiting/60)`, and per action
  `P × effectiveness − cost − risk × (1 − trust)`. Guardian's trust enters a second time in the risk
  penalty, so a risky action on doubtful data costs more; in **safe mode** (Guardian offline, or
  unverified data) risky actions are penalised further, so low-risk ones win (B4). The LLM has 8 s;
  if it is offline, late or invalid, the rules decide alone (B12). Every decision carries its score,
  the alternatives and a one-line explanation, e.g. *"rules proposal (score 7.15) chosen over llm
  (4.92): open valve-01, priority signal-01, dispatch unit-rescue-01"*.

**How it picks an actuator.** Candidate devices come from the city description: those whose
`controls` include the incident's zone (valves, signals, grid switches), and response units of the
right type. A device is never chosen if Guardian quarantined it (B8), and a unit never if it is busy,
unless the new incident's priority is at least 1.5 times the one holding it (then it is recalled
and sent). Among the rest, the score decides: effectiveness for the incident type, minus the cost of
distance in zone hops and of units used. When several incidents want the same unit, the
higher-priority incident gets it and the explanation shows the numbers (B5). If an action fails
(no acknowledgement, rejected, or no effect), the Brain decides again without that device (B6).

**Safety and execution (output).** No score can override seven rules, applied to every action in
order: S1 quarantined target; S2 all evidence rejected; S3 a risky action (valve, grid shed, unit
dispatch) needs two different agreeing sources (all calls count as one); S4 the same action on the
same device within 60 s; S5 action not allowed for the device; S6 Guardian offline: a risky action
needs two different sensor types; S7 Twin offline. A refusal is published with its rule and
reason. What passes is signed and sent; no acknowledgement within 4 s means one retry with the same
id (the Twin never applies an id twice), then an alert. After `done`, an **effect check** watches the
controlled zone: a valve must make the water fall within 30 s, else the command is marked
`no_effect`, an `actuator_no_effect` alert goes to Guardian and the Brain re-decides (B6).

**Visibility.** The graph module links every message by `correlation_id` (reading → verdict →
incident → proposals → decision → command → acknowledgement) and serves it over HTTP, also as context
for the LLM. The timeline is a web page (offline, one file) showing the three layers live: a city
map drawn from `twin/meta` (water, smoke, traffic, device states, Guardian verdicts, units driving,
command beams, safety refusals), open incidents and the last decision with its scores; each
scenario run gets one log file.

## 2.3 Tech stack and why

| Choice | Why |
|---|---|
| **Python 3.11+**, one language for every module | Team skills; pydantic models are the contracts; fast to test |
| **MQTT 3.1.1 over TLS** (Mosquitto), QoS 1 for commands | Standard IoT protocol; pub/sub fits three layers plus a timeline; retained messages and Last Will give state recovery and liveness |
| **Modules as processes on an internal topic tree** | Each sub-team built and tested alone; one crash never stops the Brain; any message can be replayed |
| **JSON + JSON Schema** (generated from pydantic, checked in CI) | Language-agnostic contracts a partner can validate against |
| **Ollama + qwen2.5-coder:7b (4-bit)** | Runs offline on the laptop GPU (no venue Wi-Fi); JSON-schema output; chosen by benchmark (below) |
| **Stdlib HTTP** for graph and timeline | No extra dependency, no CDN: works without internet |
| **Docker Compose** | One command for broker, Brain and stand-ins |

LLM benchmark on the demo laptop (RTX 5060, mains power), through the real module:

| Model | Test Book cases: valid / correct | 62 varied cases: valid / correct | Median / max | Injection runs safe |
|---|---|---|---|---|
| **qwen2.5-coder:7b** (chosen) | 100 % / 100 % | 98.7 % / 97.4 % | 2.2 s / 3.8 s | 24 / 24 |
| qwen2.5:3b | 100 % / 83 % | 100 % / 83 % | 0.9 s / 1.1 s | all |
| llama3.2:3b | 100 % / 0 % | (not run) | 0.5 s / 0.8 s | all |

## 2.4 Interfaces in / out

Full card with an example of every message: `brain/INTERFACE.md`.

| Direction | Topics | Content |
|---|---|---|
| In (Twin) | `twin/meta`, `city/{zone}/sensor/{type}/{id}`, `city/{zone}/call`, `twin/actuator/{id}`, `twin/unit/{id}`, `twin/status` | City description, readings, emergency calls (free text ≤ 280), acknowledgements and states, liveness |
| In (Guardian) | `guardian/verdict/{sensor}`, `guardian/quarantine/{device}`, `guardian/status` | Trust verdicts (trusted / suspicious / rejected + score), quarantine notices, liveness |
| Out | `brain/cmd/{device}` | Signed commands, e.g. `{"target_id": "valve-01", "action": "open", "parameters": {"opening": 1.0}}` |
| Out | `brain/event`, `brain/error`, `brain/alert`, `brain/status` | Timeline events, refused input with error code, alerts for Guardian (`actuator_no_effect`), liveness |

Pointing the Brain at another partner changes only configuration: broker address, keys, topic
patterns (`config/topics.yaml`) and field-name mappings (`config/input.yaml`).

## 2.5 Security measures

| Threat | Measure | Shown by |
|---|---|---|
| Eavesdropping, rogue client | TLS-only broker, one account per layer, password hashes | `brain/broker/`, `tools/setup.py` |
| A layer impersonating another | Topic ACL: each account writes only its own topics (a Twin account cannot publish `brain/cmd/…`) | verified on the live broker |
| Forged or altered message | HMAC-SHA256 envelope, one key per pair of layers; Ed25519 supported for Guardian | B9 (`E_AUTH`) |
| Replay, stale data | `id` remembered 60 s, `seq` per device, ±10 s window | B9 (`E_REPLAY`, `E_STALE`) |
| Malformed or impossible data | Schema, known ids, sensor range | B9 (`E_FORMAT`, `E_UNKNOWN_ID`, `E_RANGE`) |
| Plausible but false reading | Guardian verdicts weigh detection and scoring; S2, S3 | B3 / T5 (no valve, reason shown) |
| Fake calls in bulk | All calls of a zone are one source (detection, S3) | B7 |
| Prompt injection in a call | Call text in a data block it cannot close; only devices relevant to the incident type offered (no valve for an accident); answer schema enums; strict validation; parameter allowlist; then scoring and S1–S7 | B10, benchmark (all injection runs safe) |
| Compromised or broken actuator | Quarantine respected (S1); effect check, alert to Guardian | B8, B6 |
| Guardian down | Safe mode: risky actions need two sensor types (S6) | B4 |
| Smoke that is not a fire | Case memory: the gas signature is compared with 4,998 lab cases (fire vs cooking); a derived verdict, never a second source | B14 (no fire truck for burnt toast), B5 |

## 2.6 Known limitations

- **Hand-set parameters.** Detection thresholds, score tables and risk values were tuned on the mock
  city's 2 % noise, not learned from data; real sensors will need retuning (all are in `config/`).
- **One water sensor per zone.** S3 then needs a second source (residents' calls) before a valve
  opens; with Guardian offline, a flood waits for an operator (by design, B4).
- **LLM throughput.** One GPU answers one incident at a time (~2.2 s): beyond three simultaneous
  incidents the least severe go to the rule fallback. On battery the laptop GPU is too slow to meet
  the 8 s budget: the demo runs on mains power.
- **LLM judgement.** A low-severity accident sometimes gets only traffic measures; a jam whose call
  holds an injection gets no LLM proposal (safe; the rules act).
- **Effect check by direction only.** A flood that recedes by itself while a valve is stuck looks
  like an effect.
- **The decision is not undone.** Switching to a better proposal never revokes actions already
  sent, and closing an incident does not recall its units.
- **Case memory from one lab.** The library is 90 tests of one room and one material, with laboratory
  sensors, and only zones with smoke and CO sensors use it. A smouldering fire before CO rises looks
  like cooking (the known hard case of the study); with Guardian's real sensors this must be
  re-checked. The 311 study calibrates only the flood-call window (a non-emergency line, one city, one storm),
and the traffic study waits for its data.
- **Shared HMAC keys.** Any holder of a pair key can sign for that pair; Ed25519 removes this for
  Guardian.
