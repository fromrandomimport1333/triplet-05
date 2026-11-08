# MATCHING: Twin vs real Brain vs real Guardian

Status: **final, after the Twin adjustment** (8 Nov 2026). Sections 1 to 7 are the analysis made before any change (kept as written: the verdicts there are the "before"). **Section 8 gives the final status of every flow and the decisions applied.**

## 0. What was actually read

| Layer | Source read | Caveat |
|---|---|---|
| Twin | `twin/src/*.py`, `twin/city.yaml`, `twin/INTERFACE.md`, `twin/mocks/*`, `docs/schemas/` (repo `tsyp 14/tsyp`) | The `twin/` folder of the shared repo (`tsyp 144/triplet-05`) is still an empty placeholder. |
| Brain | Real code: `brain/config/*.yaml`, `src/input/*`, `src/output/service.py`, `contracts/schemas/*`, `broker/acl`, `INTERFACE.md`, `mocks/city.yaml`, kit `01-HANDOVER.md` | Full contract verified in code. |
| Guardian | **Only the spec** `Guardian - Inputs and Outputs by Layer v1.3.pdf` | **No Guardian code exists on this machine** (`triplet-05/guardian/` is empty, `tsyp 14/tsyp/guardian/` has only a README). Every Guardian statement below is "per spec", not "per code". |

## 1. Big picture

The real Brain and Guardian speak **one protocol: MQTT 3.1.1 over TLS (8883), account per layer, envelope `{"v":1,"body":"<json text>","hmac"|"hmacs":…}`**. The Twin as built speaks **REST (8000) + plaintext MQTT (1883) with an in-message `sig`**. The Twin's own stand-ins (Guardian `POST /verify`, Brain `brain/decisions`, `verdict_ref`) describe a protocol **neither real layer implements**.

So the gap is not a few field names: the whole transport and message format of the Twin must be adapted, **on the Twin side** (the Brain's `config/input.yaml` and `topics.yaml` can absorb renames, but not a different envelope, signature or transport).

MCP: **not an inter-layer channel**. The Brain's `src/mcp_server/server.py` is a read-only math toolbox (`fuse_evidence`, `detect_change_points`, `physics_*`) and says explicitly that no actuator tool exists. The Guardian says "No JSON-RPC in this profile: everything goes through MQTT". Brain makes no REST call to the Twin or the Guardian (only the LLM, timeline and graph code use `urllib`). Recommendation: **do not build an MCP server on the Twin**; keep REST as a local/test interface.

## 2. Flow matrix

Legend: OK = works as is · ADAPT = fix needed, non-blocking · BLOCK = nothing works until fixed.

### F1. Twin → Brain: **BLOCK, fix on Twin side**

| Item | Twin today | Brain expects | Fix |
|---|---|---|---|
| Transport | MQTT plaintext `127.0.0.1:1883`, optional user/pass | TLS 8883, CA `ca.crt`, account `twin` + password file | Twin: add TLS + CA + password file to config |
| Envelope / signature | flat JSON, `sig` = HMAC over sorted-keys JSON without `sig`, one key `hmac_key` | `{"v":1,"body":"<text>","hmacs":{"brain":…,"guardian":…}}`, HMAC-SHA256 over the exact body text, key = text of `.key` file as UTF-8 (not hex-decoded) | Twin: new sealer; one key per receiver (`twin-brain.key`, `twin-guardian.key`) |
| Body common fields | `contract_version`, `type`, `ts` | `id` (unique, ≤128, must survive restarts), `timestamp` (ISO UTC **ms**), `source:"twin"`, `seq` | Twin: add; `id = tw<runid>-<n>` |
| **City description** | none | `twin/meta` retained, QoS 1, **first**: zones, sensors (`min`,`max`,`alert`,`unit`), actuators (`actions`,`controls`), units. Anything not in it is `E_UNKNOWN_ID` | Twin: generate from `city.yaml` + config |
| **Reading topic** | `city/<zone>/<kind>/<SENSOR_ID>` | `city/{zone}/sensor/{type}/{sensor_id}` | Twin: new topic template |
| **IDs** | `CAM_01`, `WATER_07`, `zone_a`, `VALVE_03` | pattern `^[a-z0-9][a-z0-9-]*$` for every zone/sensor/actuator/unit id (uppercase and `_` are refused) | Twin: config id map, default `lower()` and `_`→`-` |
| Reading body | `sensor_id, zone_id, kind, value, unit, ts, seq, sig` + extras (`aqi`, camera fields, `quarantined`, `fault`) | `sensor_id, zone_id, type, value, unit, origin(real/simulated), seq`; **unit must equal the unit declared in meta**; `value` within meta `min..max` (`E_RANGE`); extra fields ignored | Twin: `kind`→`type`, add `origin:"simulated"` |
| Sensor types | `traffic_camera` (count + speed/density in extras), `water_level`, `rain`, `power_load`, `air_quality`, `emergency_call` | rule types: `water_level` m, `air_quality` ug/m3, `traffic_speed` km/h, `traffic_density` vehicles/km, `traffic_flow` vehicles/min, `power_load` kW. Other types stored, used by no rule | see section 3 |
| Calls | reading kind `emergency_call` on `city/<zone>/emergency_call/EMERGENCY_01`, `call_type` crime/fire/… | `city/{zone}/call`, `call_id` (lowercase pattern), `zone_id`, `category` ∈ fire/accident/flood/medical/other, `text` ≤280 | Twin: `crime`→`other`; zone `city` is not a declared zone, so use the nearest-zone value already computed |
| Status | none | `twin/status` retained `online`; **Last Will `offline`** set at connect. Offline ⇒ Brain sends no command (S7) | Twin: add |
| Freshness | `ts` = wall-clock UTC | within ±10 s of the Brain's clock, `id` unique 60 s, `seq` increasing per device (reset by a new `twin/status online`) | OK if NTP; Twin restart handled by status + run-id ids |
| Rate | ~42 sensors × 1 Hz (+ camera split) | tested to ~200 msg/s | OK |
| `fault` field and `city/faults` | on the bus | Brain ignores unknown fields and has no route for `city/faults`; no code reads `fault`/`truth` (grep of `src/` and `config/`: none) | OK for the Brain, but see F2 |

### F2. Twin → Guardian: **BLOCK (same adapter), plus open points**

Per spec (v1.3): same MQTT/TLS, `hmacs.guardian` verified with **`twin-guardian.key`** (the key "for you two to agree on": does it exist yet?).

| Item | Guardian expects | Verdict |
|---|---|---|
| Readings | same body as for the Brain; `seq` strictly increasing; `sensor_id` equals topic and exists in `twin/meta`; unit table `traffic_speed km/h, traffic_density vehicles/km, traffic_flow vehicles/min, water_level m, power_load kW, air_quality ug/m3` (aliases, cm/mm, veh/h, m/s converted) | same fix as F1 |
| `twin/meta` | accepted with or without a guardian tag (a wrong tag is ignored + audited); new meta id resets seq table | give `hmacs` for both |
| Acks `twin/actuator/{id}`, `twin/unit/{id}` | `result` done/rejected/failed, `correlation_id` = Brain command id. **An ack that matches no command, comes from another target, or is missing after 4+4 s counts one inconsistency; 2 ⇒ the device is quarantined** | Twin must ack every command within 4 s, never ack twice with a different target |
| Checks that can bite a calm Twin | frozen = 8 identical readings ⇒ `suspicious` (never quarantined). Twin `rain` is exactly `0.0` on a dry day and `water_level` clamps at 0.1 | ADAPT: harmless for the Brain, but verify; rain is not a Brain/Guardian type (see section 3) |
| Calls | not subscribed, never judged | OK |
| `fault`, `city/faults`, `/truth` | spec lists no such field and no REST; **cannot be verified without Guardian code** | Open question Q6. Safe default: Twin strips `fault` and stops publishing `city/faults` on the bus when integration profile is on |
| QoS | spec says QoS 1 for readings, Brain card says QoS 0 | minor: Twin publishes with a configurable QoS (default 0 for readings, 1 for meta/calls/acks) |

### F3. Brain → Twin (actuators): **BLOCK, fix on Twin side**

The Brain publishes `brain/cmd/{target_id}`, QoS 1, `hmacs` for twin + guardian:
`{"id":"brain-<session>-000042","timestamp":"…","source":"brain","correlation_id":"<reading id>","target_id":"valve-01","action":"open","parameters":{"opening":1.0}}`.
The Twin has **no MQTT command input at all** (REST `POST /actuators/{id}` with `X-API-Key`). The Brain never calls REST.

Needed in the Twin: subscribe `brain/cmd/+`; verify `hmacs.twin` (key `twin-brain.key`), freshness ±10 s; **execute each `id` at most once** (the Brain retries once with the same `id` after 4 s and the Twin must re-send the old ack); ack on `twin/actuator/{id}` or `twin/unit/{id}`: `{"actuator_id","correlation_id":<cmd id>,"result":"done|rejected|failed","state":"…","reason":"…"}`, within 4 s, `hmacs` for brain + guardian.

Action translation (Brain vocabulary comes from `config/rules.yaml`, `safety.yaml`, `scoring.yaml`; the Brain only sends actions listed in the meta `actions` of a device, units: `dispatch`, `recall`):

| Brain command | Twin native | Gap | Verdict |
|---|---|---|---|
| valve `open` `{opening:1.0}` / `close` | `VALVE_03` `{state:"open"/"closed"}` | `close`→`closed`; `opening` in 0..1 collapsed to open (>0) / closed. Twin water falls 0.05 m per sim-s, Brain effect check wants ≥0.12 m drop within 30 s of the ack | ADAPT (adapter) |
| traffic_signal `priority` / `normal` | `TL_01` `{phase:0..3}` | needs a config map `priority→phase N`, `normal→phase 0` | ADAPT (config map) |
| power_grid `shed` / `normal` | `GRID_A/B` `{action:"shed"/"normal"}` | direct. Brain effect check: `power_load` must fall ≥5 % | OK |
| unit `dispatch` `{zone_id}` | `DISPATCH_01` `{type, zone_id}`, the Twin picks the nearest free unit of that type | Brain targets one specific unit id (`unit-fire-01`); Twin has `POLICE_1, POLICE_2, AMBU_1, AMBU_2, CIVPRO_1`, no per-unit command, **and no `fire_truck` or `rescue` type** (only police, ambulance, civil_protection). Brain rules look for `fire_truck` (fire) and `rescue` (flood) | **Decision needed** (Q2): Twin declares CIVPRO as type `fire_truck`/`rescue` in meta, or Brain adds `civil_protection` to `rules.yaml`/`scoring.yaml` |
| unit `recall` | none | Twin cannot recall | ADAPT: answer `result:"rejected", reason:"unsupported_action"` (the Brain re-decides on rejection) or add a small core `recall` |
| unit status | feedback `dispatched`/`arrived` on `city/city/actuator/DISPATCH_01/state` | Brain wants `twin/unit/{unit_id}` with `status` ∈ free/en_route/on_scene/returning and `zone_id`, as a state update (no `result`) | ADAPT: emit unit status transitions; Twin needs "returning" and "free" notifications (small addition) |
| Actuators the Brain never sends | `ROAD_01` (road closure), `ALERT_01/02` (public alert) | `rules.yaml` has **no** road-closure or public-alert playbook. The Phase 0 flood playbook (close road, alert) would never run | Request to Brain (R-B2); until then do not declare them in meta, or declare them harmlessly |
| `quarantine_device` | actuator `QUARANTINE` (Guardian key) | **The Brain never asks for it**; quarantine comes only from the Guardian (F5) | not a Brain flow |
| Twin REST `/actuators`, `/tools` | kept for tests | unused by the real layers | keep, local only |
| `verdict_ref` | copied into ack/feedback when sent | the Brain command has none (it has `correlation_id`) | OK, optional, map `correlation_id`→`verdict_ref` for the audit |

### F4. Brain → Guardian: **OK (Twin not involved), but our mocks are wrong**

`brain/cmd/#` (observed, `hmacs.guardian`, key `guardian-brain.key`), `brain/alert` (`actuator_no_effect`, `actuator_not_responding`; the Brain also emits `command_rejected`, which the spec does not list), `brain/error`. All push over MQTT, **no REST**. The Twin stand-ins assume the Brain calls `POST /verify` before every action and fails closed: the real Brain never does that. `twin/mocks/` must be rewritten to the real flows (Part 2).

### F5. Guardian → Twin (quarantine / isolation): **ADAPT, non-blocking, needs requests**

- Spec: "Nothing is published for the Twin alone." The retained `guardian/quarantine/{device_id}` (`state` quarantined/released, `reason`) is the only possible signal. **The Twin account cannot read it** (ACL: twin reads only `brain/cmd/#`, `brain/status`, `brain/error`, `twin/control`). The Guardian already asked the Brain team for `topic read guardian/quarantine/#`.
- The notice is sealed with `guardian-brain.key`, single reader (`hmac`): the Twin **cannot verify it** (no key). Either the Guardian adds `hmacs.twin` with `twin-guardian.key`, or the Twin accepts it unverified (not recommended).
- The Twin's design (Guardian calls REST `POST /quarantine/{id}` with `guardian_key`) is **not used by the real Guardian**.
- Impact is small: the Brain consumes quarantine directly (S1: no command to a quarantined device), so the Twin-side flag is cosmetic. Still needed for the Phase 0 promise "the Guardian isolates a device".
- Guardian quarantines **actuators and units** too (after 2 ack inconsistencies), and the Twin `quarantine_device` accepts sensors only. Decision for Part 2: quarantined actuator ⇒ refuse commands with `result:"rejected", reason:"quarantined"` (same as the Brain mock's `reject_quarantined` mode). Please confirm (Q5).

### F6. Guardian → Brain (verdicts): **OK on paper, not the Twin's flow**

Checked against `brain/src/input/checks.py` and the schemas: `guardian/verdict/{sensor_id}` (`verdict` trusted/suspicious/rejected, `reason`, `trust_score` 0..1, `correlation_id`, `details`), retained, single `hmac` with `guardian-brain.key`, `id gd-<run>-<n>`, `guardian/quarantine/{device_id}`, `guardian/status` with Last Will. Field names, value words (`suspicious`; `suspect` is aliased) and topics match. Timing: the Brain declares the Guardian offline after **30 s** without a live verdict; the Guardian re-sends verdicts every **20 s** (v1.3 `VERDICT_REFRESH_S`): margin 10 s, OK. Two things the Twin team should know:
- **Fail-closed is not what we assumed.** Guardian offline (no verdict 30 s) ⇒ Brain **safe mode**, not a hard block: risky actions (`valve open`, `power_grid shed`, unit `dispatch`) need ≥2 different sensor types (S6) and carry a scoring penalty; low-risk actions (signal `priority`) still run. A rejected reading opens no incident; a quarantined device gets no command. No verdict yet ⇒ trust 0.6.
- Twin offline ⇒ Brain sends **no** command (S7); a Twin back `online` after `offline` resets Brain cooldowns and incidents (`reset_on_twin_restart`).

### F7. Scenario runner → Twin (`twin/control`): **ADAPT, optional**

The Brain's runner drives the mock Twin with `twin/control` messages (reset, trigger, faults, modes; sealed with `twin-brain.key`; formats in the Brain's `scenarios/README.md` and `mocks/twin/`). The Twin's equivalent is REST (`/scenario/reset`, `/scenario/conditions`, `/faults`, `/emergency_calls`). The Brain's scenarios (B1–B14, T5) reference **mock-city ids** (`zone-01`, `water-01`, `valve-01`, `unit-rescue-01`…), which do not exist in the Twin city. Without an id alias or a `twin/control` adapter, the Brain team's scenarios cannot replay against the real Twin (Q1).

## 3. Sensor mapping (decision table)

| Twin sensor | Becomes in `twin/meta` | Conversion | Open point |
|---|---|---|---|
| `traffic_camera` CAM_nn | 2 or 3 sensors: `traffic_speed` (km/h) from `mean_speed_kmh`; `traffic_density` (vehicles/km) from count ÷ road length; `traffic_flow` (vehicles/min) | speed direct. Density needs the road length (known with SUMO edges, **not exposed in `--fake`**); flow is not computed by the Twin | Q3 |
| `water_level` | `water_level`, `m`, `min 0`, `max 6` (the Twin clamps at 6.0; Brain mock uses 5 and would refuse >5), `alert` = flood level | none | Q4 (alert value) |
| `power_load` (% of feeder, 0–110) | `power_load`, `kW` | % × `capacity_kw` per feeder (new config) | capacity values |
| `air_quality` (PM2.5) | `air_quality`, `ug/m3` (exact spelling) | none; `aqi` extra dropped | `alert` value |
| `rain` (mm/h) | **no Brain rule, no Guardian unit entry** | none | Q7: expose it (stored/shown, unknown to Guardian) or leave it out |
| `emergency_call` | not a sensor: `city/{zone}/call` | `call_type` crime→other, others 1:1; `description`→`text` | none |

## 4. Other checks requested

| Check | Result |
|---|---|
| Topics, routes | Brain `brain/config/topics.yaml` `external_in` is the single list; all differences above are on our side |
| `contract_version` | Real layers do not use it (envelope `v:1`). The Twin keeps it in REST only |
| Units | water **m** (OK, no cm); air **ug/m3** (OK); power kW vs Twin %; traffic split as above |
| IDs / zones | only lowercase-hyphen ids; Twin zones `zone_a`, `zone_b`, `city`; `city` must not appear as a zone |
| HMAC | key = file text as UTF-8; sign the exact body string; body is a JSON string inside the envelope; Brain verifies `hmacs["brain"]`, Guardian `hmacs["guardian"]`, Twin verifies `hmacs["twin"]` on commands. Test vector in the handover (`test-key-do-not-use` ⇒ `503688fd…678d`) |
| Fail-closed | see F6 |
| Frequencies / timeouts | Twin ack ≤4 s (Brain), ≤4+4 s (Guardian); Brain retry once, same `id`; verdict refresh 20 s vs silence limit 30 s; freshness ±10 s |
| `/truth`, `fault`, `city/faults` | Brain: none used (verified). Guardian: unverifiable. Twin must not put them on the bus in integration mode |
| Ports | Broker 8883 (Brain host), Brain graph API 8080, timeline 8090, Twin REST 8000 (+ Twin local Mosquitto 1883, only when the Twin runs alone), Twin stand-ins 8001/8003 (alone only). No conflict. Guardian ports: unknown (spec mentions `GET /timeline`) |
| Start order | broker (Brain host) → Brain → Guardian → Twin (`twin/meta` retained first, then `twin/status online`, then readings). Twin restart: new `meta` id + run-id prefixed ids |

## 5. Priority order (today 8 Nov, joint run before 14 Nov)

**P0, blockers (Twin side, all in adapters + config)**
1. MQTT TLS + account + CA from config.
2. Envelope sealer/verifier, per-receiver keys (`hmacs` brain+guardian on outputs, verify `hmacs.twin` on commands).
3. ID normalisation (config map) and `twin/meta` generator, retained, first, plus `twin/status` and Last Will.
4. Reading + call translators (topic, fields, `origin`, units, sensor-type mapping of section 3).
5. `brain/cmd/+` subscriber, translation table of F3, dedupe by `id`, acks on `twin/actuator|unit/{id}` within 4 s.
6. Strip `fault`/`city/faults` from the bus (config flag), keep them in REST/logs for scoring.
7. Rewrite `twin/mocks/` to the real Brain and Guardian formats.

**P1**
8. Unit ids, `recall`, unit status updates; unit type naming (Q2).
9. Quarantine over MQTT (needs R-B1, R-G1); quarantined actuator behaviour.
10. Handle `brain/error` (log + surface).

**P2**
11. `twin/control` adapter or id aliasing so Brain scenarios replay (Q1).
12. Road-closure / public-alert playbooks (R-B2).
13. `rain`, `traffic_flow`.

## 6. Requests to the other teams (we do not touch their code)

**To the Brain team**
- R-B1: broker ACL: `twin` may read `guardian/quarantine/#` (and `guardian/status` if wanted).
- R-B2: add `road_closure`/`public_alert` playbooks in `rules.yaml` (flood: close road, public alert), or tell us they are out of scope.
- R-B3: confirm how `civil_protection` should be named (Q2): `fire_truck`/`rescue` in `rules.yaml`, or tell us the Twin should declare them under those names.
- R-B4: hand over privately `ca.crt`, `twin` password, `twin-brain.key`; confirm the id scheme for scenarios (Q1) and the meta `alert` values (Q4).
- R-B5: confirm density definition (`traffic_density` vehicles/km) and whether `traffic_flow` may be omitted.

**To the Guardian team**
- R-G1: add `hmacs.twin` (key `twin-guardian.key`) to `guardian/quarantine/#`, or confirm the Twin must not verify it.
- R-G2: who generates `twin-guardian.key` and how it is exchanged.
- R-G3: does the Guardian read or use a `fault` field, `city/faults` or the Twin REST `/truth`? Please confirm no (Q6).
- R-G4: behaviour for an unknown sensor type (`rain`) and for constant readings (frozen rule vs a dry-day rain of 0.0).
- R-G5: ports and native v1.0 interface (not in the PDF), and where the code will be published so we can test against it.

## 7. Open questions

1. **Which folder is the Twin?** The shared repo `tsyp 144/triplet-05/twin/` is an empty placeholder, the real Twin is in `tsyp 14/tsyp`. Part 2 and the README files target the real Twin repo unless told otherwise; the deliverables then need to be copied into `triplet-05/twin/` (CODEOWNERS) later.
2. **Guardian code:** the placeholder path was not filled in and no Guardian code is on disk. Is there a repo to read? Until then Guardian = spec only.
3. **Q1, id scheme:** the Brain's scenarios and the organizer's T5 ("fake 1.8 m at the port", `water-01`) use mock-city ids. Do we keep our Tunis ids (mapped to lowercase) and write our own scenarios, or alias to `zone-01`/`water-01`/`valve-01`?
4. **Q2, unit types:** `civil_protection` vs `fire_truck`/`rescue`; per-unit dispatch (needs an optional `unit_id` arg in the Twin core) or type-level dispatch.
5. **Q3, traffic:** density in vehicles/km and flow in vehicles/min: derive (needs road length, absent in `--fake`) or omit?
6. **Q4, alert levels** for `water_level` (Brain mock 1.3 m, Twin flood rules 1.0 m) and `air_quality`.
7. **Q5, quarantined actuator:** refuse commands with `reason:"quarantined"`?
8. **Q6, ground truth:** is dropping `fault` and `city/faults` from the bus acceptable for the demo? (Scoring then uses REST `/truth` and logs.)
9. **Q7, rain:** expose or hide.
10. Which broker host/IP will the three teams use for the joint run, and are clocks NTP-synced?

## 8. Final status after the Twin adjustment (8 Nov 2026)

### Decisions applied (the recommended answer to each open question; all are config, change them in `integration:`)

| Q | Applied | Where |
|---|---|---|
| Q1 ids | Twin keeps its Tunis ids, lower-cased (`WATER_07` -> `water-07`, `zone_a` -> `zone-a`); own scenarios later. The Brain's scenarios (mock-city ids) and the organizers' T5 (`water-01`) do **not** map yet | `integration.id_map` (override one id) |
| Q2 units | civil protection declared as `fire_truck`; per-unit dispatch through an optional `unit_id` in the Twin's dispatch; `recall` implemented. No `rescue` type: the flood playbook's dispatch finds no unit | `integration.unit_types` |
| Q3 traffic | `traffic_speed` only (one sensor per camera); density and flow not sent | `integration.sensors.traffic_camera` |
| Q4 alert levels | water 1.3 m, air 150 ug/m3 (the Brain mock's values, to confirm) | `integration.sensors.*.alert` |
| Q5 quarantined actuator/unit | later commands answered `rejected`, reason `quarantined` | `bridge.py` |
| Q6 ground truth | never on the bus (fields whitelisted, `city/faults` and `fault` dropped) | `translate.py` |
| Q7 rain | not exposed | `integration.sensors.rain.expose: false` |
| Placeholder | power: `scale 10.0` assumes a 1000 kW feeder | `integration.sensors.power_load` |

### Final status per flow

| Flow | Final status | Evidence (8 Nov, Twin `--fake` + real Brain + Brain's Guardian stand-in on a Python TLS broker) |
|---|---|---|
| Twin -> Brain | **OK** | `twin/meta` accepted (2 zones, 37 sensors, 4 actuators, 5 units), 453 messages accepted, 0 `brain/error` |
| Twin -> Guardian | **OK with the stand-in; real Guardian not tested** | stand-in verified `hmacs.guardian` and judged all 37 sensors |
| Brain -> Twin | **OK** | flood: `open valve-03` + `priority tl-01` acknowledged `done`, effect confirmed; fire: `dispatch civpro-1` acknowledged `en_route`; retried `id` applied once (unit test) |
| Brain -> Guardian | not a Twin flow | consistent on paper |
| Guardian -> Brain | **OK** (stand-in) | verdicts consumed; spoofed 4.2 m rejected, no incident opened |
| Guardian -> Twin | **Implemented, waiting for R-B1 and R-G1** | notice applied and released in the Twin with the Twin's own stand-in (readings flagged, `/truth` shows `quarantined`); with the Brain's stand-in it is refused `E_AUTH`, as designed, because that stand-in seals with another key |
| Scenario runner -> Twin (`twin/control`) | **Not done** | Q1 |
| MCP | **Not built**, not needed | section 1 |

### Finding from the joint run

With the Twin's water model, an open valve drains the canal to its 0.1 m floor, where the reading stays at exactly 0.1 for many seconds. The Brain's Guardian stand-in then judged `water-07` `rejected` / `frozen` (`same value 0.1 for 10 readings`); per the Guardian spec v1.3 the real one marks 8 identical readings `suspicious` (never quarantined). Not changed (outside this session's scope: the Twin's models are validated). Options: add a little noise at the floor, or ask the Guardian team (R-G4).

### What was changed in the Twin

`src/protocol.py`, `src/translate.py`, `src/bridge.py` (new); `src/twin.py` (hook in `publish`, `bridge.tick()`, optional `unit_id`, `recall_unit`, `command_problem`, bridge start); `src/contract.py` (env overrides); `city.yaml` (`integration:`, off by default); `mocks/partner_mock.py` and `mocks.yaml` (`wire:`); `tests/test_integration.py`. Stand-alone mode, REST, stand-ins and scenarios are untouched.

### Still open, with whom

R-B1 to R-B5 and R-G1 to R-G5 are listed in `integration/TWIN-INTEGRATION.md` section 6. Not validated: the real Guardian, and a real Mosquitto with the ACL (the test broker was a Python one without ACL).
