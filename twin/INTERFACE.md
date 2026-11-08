# Twin: Interface Card (R1)

**What this layer is:** a simulated part of central Tunis (SUMO traffic + Python models) that publishes sensor readings and executes actuator commands. It runs alone (`python twin/src/twin.py --fake`) or, with the **integration profile** (`integration.enabled: true`), as a partner of the real City Brain and Guardian, as described on this page. The older REST/plaintext interface used when running alone is kept in the appendix.

## Transport and authentication

| | |
|---|---|
| Protocol | MQTT 3.1.1 over **TLS** (port 8883 on the Brain's host), broker certificate signed by the triplet CA (`ca.crt`); account `twin` + password file. The broker ACL lets `twin` write `city/#`, `twin/#` and read `brain/cmd/#`, `brain/status`, `brain/error`, `twin/control` (plus `guardian/quarantine/#` once the Brain team grants it, request R-B1) |
| Envelope | `{"v":1,"body":"<JSON text>","hmacs":{"brain":"<hex>","guardian":"<hex>"}}`. HMAC-SHA256 over the exact UTF-8 `body` text. Key = text of the `.key` file as UTF-8, **not hex-decoded** |
| Keys | `twin-brain.key` (the Brain reads our messages and signs its commands for us), `twin-guardian.key` (the Guardian reads our messages; also used to verify quarantine notices addressed to us) |
| Freshness | `timestamp` ISO UTC with milliseconds; we accept commands within +-10 s of our clock; ids are unique (prefix = run id, so a restart is not a replay); `seq` increases per sensor |
| Liveness | `twin/status` retained `online` after connect; Last Will `offline`, retained |

**What each partner must verify:** the Brain checks `hmacs.brain` with `twin-brain.key` on everything we send; the Guardian checks `hmacs.guardian` with `twin-guardian.key`. We check `hmacs.twin` (or `hmac`) with `twin-brain.key` on `brain/cmd/#`, and with `twin-guardian.key` on `guardian/quarantine/#`.

## Outputs (Twin to Brain and Guardian)

| Topic | QoS | Ret. | Example body |
|---|---|---|---|
| `twin/meta` (sent first) | 1 | yes | `{"id":"twto7fzb-meta-000002","timestamp":"2026-11-08T22:33:11.903Z","source":"twin","zones":[{"zone_id":"zone-a","name":"Centre east","type":"commercial","exposure":"medium","adjacent":["zone-b"]}],"sensors":[{"sensor_id":"water-07","zone_id":"zone-a","type":"water_level","unit":"m","min":0,"max":6,"alert":1.3}],"actuators":[{"actuator_id":"valve-03","type":"valve","zone_id":"zone-a","controls":["zone-a"],"actions":["open","close"]}],"units":[{"unit_id":"civpro-1","type":"fire_truck","home_zone":"zone-a"}]}` |
| `city/{zone}/sensor/{type}/{sensor_id}` | 0 | no | `{"id":"twto7fzb-water-07-126","timestamp":"2026-11-08T22:35:18.267Z","source":"twin","sensor_id":"water-07","zone_id":"zone-a","type":"water_level","value":0.407,"unit":"m","origin":"simulated","seq":126}` |
| `city/{zone}/call` | 1 | no | `{"id":"twto7fzb-call-000011","timestamp":"2026-11-08T22:34:02.609Z","source":"twin","call_id":"call-000040","zone_id":"zone-a","category":"flood","text":"Water over the road near the canal"}` |
| `twin/actuator/{id}` | 1 | no | acknowledgement: `{"id":"twto7fzb-act-000009","timestamp":"2026-11-08T22:33:46.269Z","source":"twin","correlation_id":"brain-to7fyr-000001","actuator_id":"valve-03","state":"open","result":"done"}` (`result` done / rejected / failed, plus `reason` when not done). Without `correlation_id` and `result` it is a plain state change |
| `twin/unit/{id}` | 1 | no | `{"id":"twto7fzb-unit-000014","timestamp":"2026-11-08T22:36:25.920Z","source":"twin","correlation_id":"brain-to7fyr-000003","unit_id":"civpro-1","result":"done","status":"en_route","zone_id":"zone-b","state":"dispatched"}`; later plain updates `on_scene`, `returning`, `free` |
| `twin/status` | 1 | yes | `{"id":"twto7fzb-status-000003","timestamp":"2026-11-08T22:33:11.903Z","source":"twin","status":"online"}` |

Sensors declared by default (`integration.sensors`): `traffic_speed` km/h (one per camera, id `cam-NN-speed`), `water_level` m, `air_quality` ug/m3, `power_load` kW (converted from % with `scale`). Rain, traffic density and traffic flow are not exposed (MATCHING.md Q3, Q7). Ground truth never leaves the Twin: no `fault` field, no `city/faults`, no `/truth`.

## Inputs (Brain and Guardian to Twin)

| Topic | From | Example body | What the Twin does |
|---|---|---|---|
| `brain/cmd/{target_id}` | Brain (`hmacs.twin`) | `{"id":"brain-to7fyr-000001","timestamp":"2026-11-08T22:33:45.326Z","source":"brain","correlation_id":"twto7fzb-water-07-32","target_id":"valve-03","action":"open","parameters":{"opening":1.0}}` | translates it (table below) and acknowledges within 4 s; the **same `id` is applied once** and a retry gets the same answer |
| `guardian/quarantine/{device_id}` | Guardian | `{"id":"gd-1-000007","timestamp":"2026-11-08T22:36:00.000Z","source":"guardian","device_id":"water-07","state":"quarantined","reason":"implausible_jump"}` | sensor: flagged inside the Twin; actuator or unit: later commands answered `rejected`, reason `quarantined`. `released` lifts it |
| `brain/error` | Brain | `{"code":"E_FORMAT","topic":"...","message_id":"...","detail":"..."}` | logged with the cause |

| Brain action | Twin native | Notes |
|---|---|---|
| `valve-NN open` / `close` | `VALVE_NN` state `open` / `closed` | `opening` 0 closes |
| `tl-NN priority` / `normal` | `TL_NN` phase 2 / 0 | `integration.actuators.traffic_signal.map` |
| `grid-X shed` / `normal` | `GRID_X` action | |
| `<unit> dispatch {zone_id}` | `DISPATCH_01` with that `unit_id` | busy unit: `failed`, reason `no_unit ... busy` |
| `<unit> recall` | frees the unit now | acknowledged `done`, status `returning`, then `free` |

## Errors (what the Twin does with a bad message)

| Problem | Reaction |
|---|---|
| Bad envelope, wrong key, no `hmacs.twin`, clock skew over 10 s, missing field, topic and body disagree | Command dropped, one log line `refused brain/cmd/...: E_AUTH, E_STALE or E_FORMAT, with the cause`; nothing is executed. The Twin cannot write to `brain/error` (only the Brain publishes there) |
| Unknown target id | `E_UNKNOWN_ID`, dropped, logged |
| Valid command the Twin cannot execute (unknown action, unknown zone, invalid args, quarantined device) | Acknowledgement `result:"rejected"` with a `reason`, for example `unsupported_action: valve-03 accepts open, close, got teleport` |
| Actuator fails (injected `actuator_stuck`) | Acknowledgement `result:"failed"`. A silent stuck actuator acknowledges `done` with no effect, by design |
| The Brain refuses one of our messages | `brain/error` is logged: `BRAIN REFUSED one of our messages: E_RANGE ...` |

## Pointing the Twin at another partner (configuration only)

Everything is under `integration:` in `twin/city.yaml`; environment variables win (`INTEGRATION_ENABLED`, `PARTNER_MQTT_HOST`, `PARTNER_MQTT_PORT`, `PARTNER_MQTT_USERNAME`, `PARTNER_MQTT_PASSWORD_FILE`, `PARTNER_MQTT_CA_CERT`, `PARTNER_MQTT_TLS`, `TWIN_BRAIN_KEY_FILE`, `TWIN_GUARDIAN_KEY_FILE`).

| What changes | Key |
|---|---|
| Broker address, port, TLS, CA, account, password file | `integration.mqtt.host`, `port`, `tls`, `ca_cert`, `username`, `password_file`, `client_id` |
| HMAC keys | `integration.keys.brain`, `integration.keys.guardian` |
| Device and zone names (partners accept only `[a-z0-9][a-z0-9-]*`) | `integration.id_map` (default: lower case, `_` to `-`) |
| Which sensors, their type, unit, range, alert level, conversion | `integration.sensors` (`type`, `unit`, `field`, `scale`, `min`, `max`, `alert`, `expose`) |
| Which actuators are declared, their action names and translation | `integration.actuators` (`type`, `actions`, `map`) |
| Unit type names, call categories | `integration.unit_types`, `integration.call_categories` |
| Zone type, exposure, adjacency | `integration.zones` |
| Clock tolerance, readings QoS, run id | `clock_window_s`, `qos_readings`, `run_id` |

The standalone interface (REST on port 8000, the Brain, Guardian and test keys, faults) is unchanged and is still how the tests drive the Twin; see the appendix.

## Appendix: native stand-alone interface (REST + plaintext MQTT, used when integration.enabled is false)

**What this layer is:** a simulated city of central Tunis (SUMO traffic + Python models) that publishes signed sensor readings and executes actuator commands. It runs alone (`python twin/src/twin.py --fake`), with no Brain or Guardian needed.

All messages are JSON with `"contract_version": "1.0"`. Times are UTC ISO-8601. One JSON Schema per message type is in [`docs/schemas/`](../docs/schemas/) (`reading`, `command`, `ack`, `feedback`, `verdict`, `decision`, `error`, `verify_request`). Zones and device lists: `GET /devices`. Zones are `zone_a` (east) and `zone_b` (west); city-wide devices use zone `city`.

### A1. What the Twin sends (MQTT, port 1883)

| Topic | When | Message type |
|---|---|---|
| `city/<zone>/<kind>/<sensor_id>` | every `publish_every_s` (1 s) of simulated time per sensor; per-kind override `publish_every_s_by_kind` | `reading` |
| `city/city/emergency_call/EMERGENCY_01` | random (`calls_per_hour`), or on `POST /emergency_calls` | `reading` (kind `emergency_call`) |
| `city/<zone>/actuator/<actuator_id>/ack` | when a command is accepted | `ack` |
| `city/<zone>/actuator/<actuator_id>/state` | when a command has taken effect (or failed); dispatch also when the unit arrives | `feedback` |
| `city/faults` | when a test fault starts or stops | `fault_event` (scoring only, see below) |

#### Sensors (Phase 0 report, Table 1)

| Family | `kind` | `value` unit | Rate | Extra fields | Devices |
|---|---|---|---|---|---|
| Traffic cameras | `traffic_camera` | vehicles on the road | 1 Hz | `mean_speed_kmh`, `halted_vehicles`, `density_pct` (% of the road's capacity) | `CAM_01`..`CAM_29` |
| Water level (upstream/downstream pair) | `water_level` | m | 1 Hz | none; `/devices` shows `pair`, `position`, `valve` | `WATER_07` (upstream), `WATER_08` (downstream), served by `VALVE_03` |
| Rain | `rain` | mm/h | 1 Hz | none | `RAIN_01`..`RAIN_05` |
| Power load | `power_load` | % of feeder capacity (0 to 110) | 1 Hz | none | `POWER_01` (zone_b), `POWER_02` (zone_a) |
| Air quality | `air_quality` | PM2.5 µg/m³ | 1 Hz (Table 1 says 0.2 Hz; set `publish_every_s_by_kind: {air_quality: 5}` to follow it) | `aqi` (US EPA index computed from PM2.5) | `AIR_01`..`AIR_04` |
| Emergency calls | `emergency_call` | 1 per call | event | `call_id`, `call_type`, `service`, `number_dialled`, `severity`, `description`, `lonlat`, `road`, `sim_time` | `EMERGENCY_01` |

In `--fake` mode camera details are simulated consistently from the vehicle count; with SUMO they come from the road (`traci`). A sensor quarantined by the Guardian keeps publishing, with `"quarantined": true` inside the signed part, so the Guardian can keep watching it and release it.

**Reading (example):**
```json
{"contract_version":"1.0","type":"reading","sensor_id":"CAM_02","zone_id":"zone_b","kind":"traffic_camera","value":28,"unit":"vehicles","ts":"2026-11-08T09:31:44.760Z","seq":78,"mean_speed_kmh":6.3,"halted_vehicles":24,"density_pct":93.3,"sig":"<hmac>"}
```

#### Actuators (Phase 0 report, Table 2)

| Actuator | Devices | `args` for `POST /actuators/<id>` | Tool (`POST /tools/<tool>`) | Feedback `state` | Effect in the simulation |
|---|---|---|---|---|---|
| Water valve | `VALVE_03` (zone_a) | `{"state":"open"\|"closed"}` | `set_water_valve(zone_id, state)` | `open`, `closed` | Open: the canal level falls 0.05 m per simulated second |
| Traffic signal | `TL_01` (zone_a) | `{"phase":0..3}` | `set_traffic_signal(zone_id, phase)` | `phase_<n>` | SUMO signal phase |
| Road closure | `ROAD_01` (zone_a) | `{"state":"closed"\|"open"}` | `set_road_closure(zone_id, state)` | `closed`, `open` | SUMO: cars may no longer use the road |
| Public alert | `ALERT_01` (zone_a), `ALERT_02` (zone_b) | `{"level":"info"\|"yellow"\|"orange"\|"red","msg":"1 to 280 characters"}` | `broadcast_alert(zone_id, level, text)` | `alert_<level>` | Printed by the Twin; carried in the feedback (`level`, `msg`) |
| Power grid | `GRID_A` (zone_a), `GRID_B` (zone_b) | `{"action":"normal"\|"reroute"\|"shed"}` | `set_power_grid(zone_id, action)` | the action | That zone's `power_load` readings x1.0 / x0.8 / x0.65 |
| Emergency dispatch | `DISPATCH_01` (city) | `{"type":"police"\|"ambulance"\|"civil_protection","zone_id":"zone_a"}` | `dispatch_emergency(type, zone_id)` | `dispatched`, later `arrived`; `no_unit` if none is free | The nearest free unit drives to the zone centre. ETA = SUMO route length / 40 km/h, or straight line x 1.3 in `--fake` mode. Feedback carries `unit_id`, `unit_type`, `target_zone`, `eta_s`, `distance_m`, `route` (`sumo_route` or `estimate`). The unit is busy until `on_scene_s` after arrival |
| Device quarantine | `QUARANTINE` (city) | `{"sensor_id":"AIR_01","state":"quarantined"\|"released"}` | `quarantine_device(sensor_id)`, `release_device(sensor_id)` | `quarantined`, `released` (+ `sensor_id`) | Readings of that sensor carry `"quarantined": true`. **Guardian key only** |

Every command gets an **ack** in the HTTP reply (also published on `.../ack`), then a **feedback** message once applied. If the command is valid but the actuator fails (`actuator_stuck` fault, no free unit), the feedback has `"ok": false` and a `detail`.

**Ack (HTTP reply) and feedback (MQTT):**
```json
{"contract_version":"1.0","type":"ack","cmd_id":"brain-12","actuator_id":"VALVE_03","zone_id":"zone_a","accepted":true,"verdict_ref":"V-3f2a","ts":"...","note":"result is published as feedback on MQTT city/zone_a/actuator/VALVE_03/state","sig":"<hmac>"}
{"contract_version":"1.0","type":"feedback","cmd_id":"brain-12","actuator_id":"VALVE_03","zone_id":"zone_a","state":"open","ok":true,"detail":"","verdict_ref":"V-3f2a","ts":"...","sig":"<hmac>"}
{"contract_version":"1.0","type":"feedback","cmd_id":"brain-15","actuator_id":"DISPATCH_01","zone_id":"city","state":"dispatched","ok":true,"detail":"CIVPRO_1 on its way to zone_a, about 52 s","unit_id":"CIVPRO_1","unit_type":"civil_protection","target_zone":"zone_a","eta_s":51.7,"distance_m":574,"route":"estimate","ts":"...","sig":"<hmac>"}
```

#### Fault labels (for scoring only)

While a test fault changes a message, that message carries `"fault": {"kind":"spoof","id":"F-..."}` inside the signed part. `city/faults` carries `fault_event` messages (`event` = `start` or `stop`, with `fault_id`, `sensor_id`, `kind`, `value`, times and `reason`). **A Guardian being tested must ignore the `fault` field and `city/faults`**: they reveal the answer. A replayed message is an unchanged old message, so it has no label; use `city/faults` or `/truth` to score it.

### A2. What the Twin receives (REST, port 8000, docs at `/docs`)

| Call | Body | Key | Who uses it |
|---|---|---|---|
| `GET /health`, `GET /contract`, `GET /devices`, `GET /sensors/{id}`, `GET /tools`, `GET /quarantine` | none | none | anyone. `/contract` lists the contract version, topics, endpoints, key roles and schemas |
| `POST /actuators/{id}` | `{"cmd_id":"c1","args":{...},"verdict_ref":"V-..."}` | **Brain** (`api_key`); `device_quarantine`: **Guardian** (`guardian_key`) | Brain |
| `POST /tools/{tool}` | `{"cmd_id":"c1","args":{"zone_id":"zone_a","state":"open"},"verdict_ref":"..."}` | as above | Brain, MCP adapter |
| `GET /actuators/{id}`, `GET /emergency_calls` | none | Brain or test | Brain, tests |
| `POST /quarantine/{sensor_id}`, `DELETE /quarantine/{sensor_id}` | none | **Guardian only** | Guardian |
| `POST /faults`, `DELETE /faults/{id}`, `GET /faults` | `{"sensor_id":"WATER_07","kind":"spoof","value":4.2,"duration_s":60}` | **test only** (`test_key`) | test scripts |
| `POST /scenario/rain/{mm_h}` | none | test only | test scripts |
| `POST /scenario/conditions`, `GET /scenario/conditions` | `{"zone_id":"zone_a","rain_mm_h":60,"jam":1.0,"pollution":8,"power_demand":1.7}` (all optional; no `zone_id` = every zone) | test only | test scripts |
| `POST /scenario/reset` | none: calm weather, no jam, normal demand, faults cleared, water back to 0.4 m, valves closed, roads open, grids normal, units back at base (feedback `cmd_id: "scenario-reset"` for each actuator it changes). Quarantine is left to the Guardian | test only | test scripts |
| `POST /emergency_calls` | `{"call_type":"fire","lonlat":[10.18,36.80],"severity":"high"}` | test only | test scripts, call-centre mock |
| `GET /truth/{id}`, `GET /events` | none | test only | scoring; **the Brain and Guardian cannot call these** |

**Fault kinds** (`POST /faults`):

| `kind` | `value` | Effect |
|---|---|---|
| `stuck` | none | Repeats the last value (cameras: all fields) |
| `dropout` | none | Publishes nothing |
| `spoof`, `spike` | the fake value (required) | Publishes that value; camera speed, stopped vehicles and density follow the fake count |
| `drift` | rate per simulated second (required) | Publishes truth + rate x seconds since the start: a slow linear offset |
| `replay` | none | Holds back the genuine reading and re-sends the last 30 genuine signed messages unchanged (old `seq`, old `ts`), in a loop. The device keeps counting, so its `seq` jumps when the attack ends |
| `flood_network` | extra messages per reading, 1 to 200 (default 20) | The device publishes many correctly signed readings per second |
| `actuator_stuck` | none: refuses (`ok:false`); `1`: **silent**, reports success but has no effect | For actuators only |

Ground truth for every sensor stays in `GET /truth/{id}` (`true_value`, `true_fields` for cameras, `active_fault`, `quarantined`).

### A3. How to point it at a different partner

Nothing is hard-coded. Edit `twin/city.yaml` (`mqtt.host`, `mqtt.port`, `mqtt.username`, `mqtt.password`, `api.host`, `api.port`) or set environment variables, which win over the file: `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `TWIN_API_HOST`, `TWIN_API_PORT`, `TWIN_API_KEY`, `TWIN_GUARDIAN_KEY`, `TWIN_TEST_KEY`, `TSYP_HMAC_KEY` (template: `.env.example`). To check a partner layer against this contract: `python twin/tools/contract_check.py --provoke` (see `integration/TWIN-INTEGRATION.md`). A partner only needs the broker address, the topic names above and the REST address.

### A4. Authentication

- **REST:** header `X-API-Key`, one key per layer (least privilege, Phase 0 report section 8.2):

  | Key (`twin/city.yaml`, env) | Role | May | May not |
  |---|---|---|---|
  | `api.api_key` (`TWIN_API_KEY`) | City Brain | command every actuator; read actuator state and emergency calls | quarantine, faults, conditions, `/truth`, `/events` |
  | `api.guardian_key` (`TWIN_GUARDIAN_KEY`) | Guardian | quarantine and release devices | anything else |
  | `api.test_key` (`TWIN_TEST_KEY`) | test scripts, scoring | faults, conditions, reset, emergency calls, `/truth`, `/events`, read actuator state | command actuators |

  A key used outside its role gets **HTTP 403** with the reason; a missing or unknown key **HTTP 401**.
- **MQTT:** username and password (Mosquitto password file), when configured.
- **Message integrity:** `sig` = HMAC-SHA256, hex, over the message JSON **without** the `sig` field, serialised with sorted keys and compact separators (`json.dumps(msg, sort_keys=True, separators=(",", ":"))`), keyed with `api.hmac_key`. Receivers should reject a bad `sig`, a repeated or older `seq` for the same `sensor_id`, and a `ts` more than a few seconds from their clock.
- Commands may carry `verdict_ref` (the Guardian verdict the decision relied on). The Twin copies it into the ack and the feedback, so the audit trail links action to verdict. The Twin does not itself refuse commands without one.

### A5. Validation and errors

Command bodies are validated against `docs/schemas/command.schema.json`, then the `args` against the actuator's rules. Invalid input gets **HTTP 422** and nothing is queued. Every error reply uses the contract's error message, with `detail` as plain text:

```json
{"contract_version":"1.0","type":"error","code":"BAD_ARGS","detail":"ALERT_01: args.level must be one of info, yellow, orange, red, got \"purple\"","ts":"..."}
```

`code` is `SCHEMA_INVALID` (body does not match a schema), `BAD_ARGS` (valid shape, invalid values), `UNAUTHORIZED` (401), `FORBIDDEN` (403) or `NOT_FOUND` (404).
