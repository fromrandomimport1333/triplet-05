# City Brain: Interface Card (R1)

What the Brain receives and sends, one example of each message, how to point it at another
partner, and how it authenticates. JSON Schemas of every body: `contracts/schemas/`.

## Transport and authentication

| | |
|---|---|
| **Protocol** | MQTT 3.1.1 over **TLS** (port 8883), broker certificate signed by the triplet CA (`secrets/ca.crt`). No plaintext listener. |
| **Broker account** | One per layer (`brain`, `twin`, `guardian`), password files in `secrets/`. A **topic ACL** lets each layer write only its own topics (`brain/broker/acl`): a Twin account cannot publish `brain/cmd/…`. |
| **Message signature** | Every payload is an envelope `{"v": 1, "body": "<JSON text>", "hmac": "<hex>"}`, or `"hmacs": {"brain": …, "guardian": …}` when several layers read it. HMAC-SHA256 over the UTF-8 body text, **one key per pair of layers** (`secrets/keys/twin-brain.key`, `guardian-brain.key`). Ed25519 for Guardian is supported (`security.yaml: guardian_signature.mode`). |
| **Freshness / replay** | `timestamp` within ±10 s, `id` unique for 60 s, `seq` increasing per device (if sent). Retained and Last-Will messages skip freshness/replay, never the signature. |
| **Liveness** | Every layer publishes `<layer>/status` `online`, Last Will `offline` (retained). |

Setup: `cd brain && python tools/setup.py` creates the CA, broker certificate, keys and accounts.

## Inputs (Twin and Guardian → Brain)

Common body fields: `id`, `timestamp` (ISO 8601 UTC, ms), `source` (`twin` / `guardian`), optional `correlation_id`, `seq`.

| Topic | QoS | Ret. | Body example (common fields omitted after the first) |
|---|---|---|---|
| `twin/meta` | 1 | yes | `{"id":"tw-000004","timestamp":"2026-11-14T10:00:00.000Z","source":"twin","zones":[{"zone_id":"zone-01","name":"Port","type":"port","exposure":"high","adjacent":["zone-02"]}],"sensors":[{"sensor_id":"water-01","zone_id":"zone-01","type":"water_level","unit":"m","min":0,"max":5,"alert":1.3}],"actuators":[{"actuator_id":"valve-01","type":"valve","zone_id":"zone-01","controls":["zone-01"],"actions":["open","close"]}],"units":[{"unit_id":"unit-ambulance-01","type":"ambulance","home_zone":"zone-04"}]}` |
| `city/{zone}/sensor/{type}/{sensor_id}` | 0 | no | `{"sensor_id":"water-01","zone_id":"zone-01","type":"water_level","value":1.62,"unit":"m","origin":"simulated","seq":182}` |
| `city/{zone}/call` | 1 | no | `{"call_id":"call-tw17-0042","zone_id":"zone-03","category":"accident","text":"Two cars collided at the junction"}` (text ≤ 280) |
| `twin/actuator/{id}`, `twin/unit/{id}` | 1 | state | `{"actuator_id":"valve-01","correlation_id":"brain-000042","result":"done","state":"open_100"}` · unit: `{"unit_id":"unit-fire-01","status":"en_route","zone_id":"zone-05"}` |
| `guardian/verdict/{sensor_id}` | 1 | yes | `{"sensor_id":"water-01","correlation_id":"tw17-000182","verdict":"rejected","reason":"implausible_jump","trust_score":0.02}` (`trusted` / `suspicious` (or `suspect`) / `rejected`) |
| `guardian/quarantine/{device_id}` | 1 | yes | `{"device_id":"valve-01","state":"quarantined","reason":"inconsistent_ack"}` (`released` to lift it) |
| `twin/status`, `guardian/status` | 1 | yes | `{"status":"online"}` (`offline` / `degraded`; Last Will `offline`) |

**Refused messages** are reported on `brain/error` (never silently dropped):

| Code | When |
|---|---|
| `E_FORMAT` | Bad envelope/JSON, missing or wrong-type field, wrong unit, topic and body disagree, wrong `source` |
| `E_AUTH` | HMAC (or Ed25519) missing or wrong |
| `E_STALE` | `timestamp` more than 10 s from the Brain's clock |
| `E_REPLAY` | Same `id` within 60 s, or `seq` not increasing |
| `E_UNKNOWN_ID` | Zone, sensor, actuator, unit or device not in `twin/meta` |
| `E_RANGE` | Reading outside the sensor's `min` / `max` |

Unknown extra fields are ignored. Guardian is considered offline after 30 s without a live verdict.

**Sensor types the Brain understands** (the type names of `twin/meta`; others are stored and shown, but used by
no incident rule; names are configuration in `config/detection.yaml` and `config/memory.yaml`):
`water_level` (m), `air_quality` (ug/m3), `traffic_speed`, `traffic_density`, `traffic_flow`, `power_load` (kW),
and, optional, a **fire signature** per zone: `smoke` (obscuration, %/ft), `co` (ppm), `co2` (ppm),
`temperature` (deg C). Where a zone has at least `smoke` and `co`, the case memory compares their last
minute with 4,998 NIST lab cases to tell a real fire from cooking (B14). Without them, fires are
detected from air quality and calls as before.

## Outputs (Brain → Twin, Guardian, timeline)

| Topic | QoS | Ret. | Body example |
|---|---|---|---|
| `brain/cmd/{target_id}` (to the Twin, `hmacs` twin + guardian) | 1 | no | `{"id":"brain-000042","timestamp":"…","source":"brain","correlation_id":"tw17-000182","target_id":"valve-01","action":"open","parameters":{"opening":1.0}}` · unit: `{"target_id":"unit-fire-01","action":"dispatch","parameters":{"zone_id":"zone-05"}}` |
| `brain/event` (timeline) | 0 | no | `{"id":"math-000020","timestamp":"…","source":"brain","correlation_id":"tw17-000182","event":"decision","incident_id":"inc-0031","summary":"llm proposal (score 1.53) chosen over rules (1.21): open valve-01"}` |
| `brain/error` | 0 | no | `{"id":"in-err-00031","timestamp":"…","source":"brain","code":"E_RANGE","topic":"city/zone-01/sensor/water_level/water-01","message_id":"tw17-000199","detail":"value -5.0 outside [0, 5]"}` |
| `brain/alert` (to Guardian) | 1 | no | `{"id":"brain-000051","timestamp":"…","source":"brain","alert":"actuator_no_effect","target_id":"valve-01","detail":"water still rising 30 s after open"}` |
| `brain/status` | 1 | yes | `{"status":"online"}` (Last Will `offline`) |

A command is acknowledged by the Twin on `twin/actuator/{id}` or `twin/unit/{id}` with
`correlation_id` = the command `id`; no answer within 4 s → retried once with the same `id`
(the Twin executes an `id` at most once), then reported failed and alerted.

## Pointing the Brain at another partner (configuration only, nothing hard-coded)

| What changes | Where |
|---|---|
| Broker address, port, client ids | `config/broker.yaml` |
| CA, client certificate (mTLS), account, HMAC key files, Ed25519 | `config/security.yaml` |
| Topic names (e.g. the partner uses `twin/events/#`) | `config/topics.yaml` (`external_in` patterns) |
| Field names (`event_id` → `id`) and value aliases (`suspect` → `suspicious`) | `config/input.yaml` (`field_map`, `verdict_aliases`) |
| City: zones, sensors, actuators, units, allowed actions | learned from the partner's `twin/meta`, never configured |
| Deployment-specific values (e.g. Docker) | a partial file in `config/overlays/<name>/` + `BRAIN_CONFIG_OVERLAY` |
