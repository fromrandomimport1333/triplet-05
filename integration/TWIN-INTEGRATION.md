# Integration: how the Twin, the City Brain and the Guardian connect

The Twin team owns `twin/`, the City Brain team `brain/`, the Guardian team `guardian/`. This file describes the **real** wiring between the three layers as implemented on 8 November 2026. The Twin adapts to the partners' protocol through configuration and adapters (`twin/src/protocol.py`, `translate.py`, `bridge.py`); the analysis behind every choice is in [`MATCHING.md`](MATCHING.md). Interface cards: [`../twin/INTERFACE.md`](../twin/INTERFACE.md), [`../brain/INTERFACE.md`](../brain/INTERFACE.md), [`../guardian/INTERFACE.md`](../guardian/INTERFACE.md).

The Twin can still run alone with its own stand-ins and the older REST contract (appendix of `twin/INTERFACE.md`, `docker-compose.twin.yml`, scenarios T1 to T12). Everything below is the **integration profile** (`integration.enabled: true`).

## 1. Architecture

```text
                    MQTT broker, TLS :8883  (on the Brain's host; one account per layer, topic ACL)
   +--------------------------------------------------------------------------------------------+
   | twin/meta  city/{z}/sensor/{t}/{id}  city/{z}/call  twin/actuator/{id}  twin/unit/{id}      |
   | twin/status      guardian/verdict/{id}  guardian/quarantine/{id}  guardian/status           |
   | brain/cmd/{id}   brain/alert  brain/error  brain/event  brain/status                        |
   +---^---------------------^----------------------------^------------------------^-----------+
       | publishes           | publishes                  | publishes              | reads all
  +----+---------+      +----+----------+            +----+---------+        +-----+----------+
  |    TWIN      |      |   GUARDIAN    |            |  CITY BRAIN  |        | timeline :8090 |
  | SUMO/--fake  |      | trust, quar.  |            | detect,      |        | graph API:8080 |
  | REST :8000   |      +---------------+            | decide, act  |        +----------------+
  | (tests only) |                                   +--------------+
  +--------------+
   Twin -> Brain, Guardian : meta, readings, calls, acks, unit status   (sealed for both)
   Guardian -> Brain       : verdicts, quarantines, status              (sealed for the Brain)
   Guardian -> Twin        : quarantine notices (needs ACL R-B1, hmacs.twin R-G1)
   Brain -> Twin, Guardian : commands (sealed for both); Brain -> Guardian: alerts
   Scenarios/tests -> Twin : REST :8000 with the test key (faults, conditions, reset, /truth)
```

Everything between the layers is MQTT. There is no REST and no MCP between layers (the Brain's MCP server is a read-only math toolbox; the Guardian states that everything goes through MQTT).

## 2. Protocol per flow (final status)

Common to all: MQTT 3.1.1 over TLS, CA `ca.crt`, one account per layer, envelope `{"v":1,"body":"<JSON text>","hmac"|"hmacs":{...}}`, HMAC-SHA256 over the exact UTF-8 body text with the key file's text (not hex-decoded), body fields `id`, `timestamp` (ISO UTC, ms, within +-10 s), `source`.

| Flow | Topic / endpoint | Body (fields, units) | Authentication | Status |
|---|---|---|---|---|
| Twin -> Brain | `twin/meta` (retained, first); `city/{zone}/sensor/{type}/{id}` QoS 0; `city/{zone}/call` | meta: zones, sensors (`min`,`max`,`alert`,`unit`), actuators, units. Reading: `sensor_id, zone_id, type, value, unit, origin, seq`. Units: water **m**, air **ug/m3**, traffic speed **km/h**, power **kW** | `hmacs.brain`, `twin-brain.key` | **Working**, verified against the real Brain: 0 `brain/error` |
| Twin -> Guardian | same topics, `twin/actuator/{id}`, `twin/unit/{id}` | same bodies | `hmacs.guardian`, `twin-guardian.key` | **Working against the Brain's Guardian stand-in; real Guardian untested** (no code available, spec v1.3 only). Key `twin-guardian.key` to be agreed |
| Brain -> Twin | `brain/cmd/{target_id}` QoS 1 | `id, correlation_id, target_id, action, parameters`; acks on `twin/actuator/{id}` or `twin/unit/{id}` within 4 s | `hmacs.twin`, `twin-brain.key` | **Working**: valve, signal, unit dispatch and recall. Roads and public alerts are not commanded by the Brain (R-B2) |
| Brain -> Guardian | `brain/cmd/#`, `brain/alert`, `brain/error` (observed) | as above | `hmacs.guardian`, `guardian-brain.key` | Not a Twin flow; consistent in the Guardian spec and the Brain code |
| Guardian -> Brain | `guardian/verdict/{sensor_id}` (retained), `guardian/quarantine/{id}`, `guardian/status` | `verdict` trusted/suspicious/rejected, `reason`, `trust_score` 0..1, `correlation_id` = reading `id` | single `hmac`, `guardian-brain.key` | **Working** with the Brain's Guardian stand-in; consistent with the spec. Verdict refresh 20 s vs Brain limit 30 s |
| Guardian -> Twin | `guardian/quarantine/{device_id}` | `device_id, state, reason` | needs `hmacs.twin` with `twin-guardian.key` | **Implemented in the Twin and tested; blocked on two requests**: R-B1 (ACL: twin may read `guardian/quarantine/#`), R-G1 (Guardian adds `hmacs.twin`) |
| Scenario runner -> Twin | `twin/control` | reset, trigger, faults | `twin-brain.key` | **Not implemented** (MATCHING.md Q1). Tests use the Twin's REST API |

Twin REST (`:8000`) stays for tests: keys `api_key` (Brain), `guardian_key`, `test_key`; unchanged.

## 3. One scenario traced end to end: real flood (Twin + real Brain + Guardian stand-in)

Run of 8 Nov 2026, 22:33 UTC. `POST /scenario/conditions {"zone_id":"zone_a","rain_mm_h":90}` raises the canal; times are from the Brain's events.

| Time (UTC) | Layer | Message (topic: essential fields) |
|---|---|---|
| 22:33:37.316 | Guardian | `guardian/verdict/water-07`: `{"verdict":"trusted","reason":"consistent","trust_score":0.95,"correlation_id":"twto7fzb-water-07-26"}` |
| 22:33:45.303 | Brain | `brain/event` `incident_opened`: *flood opened at zone-a Centre east (confidence 0.84, high)*, `correlation_id` `twto7fzb-water-07-32` (the reading that triggered it) |
| 22:33:45.324 | Brain | `decision`: *rules proposal chosen: open valve-03, priority tl-01; no free rescue* |
| 22:33:45.326 | Brain -> Twin | `brain/cmd/valve-03`: `{"id":"brain-to7fyr-000001","source":"brain","correlation_id":"twto7fzb-water-07-32","target_id":"valve-03","action":"open","parameters":{"opening":1.0}}` and `brain/cmd/tl-01` `priority` |
| 22:33:45 | Twin | `[bridge] command brain-to7fyr-000001: open valve-03 -> VALVE_03 {"state": "open"}` |
| 22:33:46.269 | Twin -> Brain | `twin/actuator/valve-03`: `{"correlation_id":"brain-to7fyr-000001","actuator_id":"valve-03","state":"open","result":"done"}`; `twin/actuator/tl-01` `phase_2` `done` |
| 22:33:46.274 | Brain | `command_acknowledged` for both commands |
| 22:33:46.685 | Brain | `effect_confirmed` for the signal: *cam-17-speed rose from 42.4 to 46.5* |
| 22:33:54.699 | Brain | `effect_confirmed` for the valve: *water-07 fell from 0.507 to 0.385*, then `incident_closed` |

Attack variant (same run): `POST /faults {"sensor_id":"WATER_07","kind":"spoof","value":4.2}` -> `guardian/verdict/water-07` `rejected` `implausible_jump` (0.05) -> `guardian/quarantine/water-07` `quarantined`; the Brain opened **no** flood incident and sent no command.

## 4. Shared decisions

| Decision | Value |
|---|---|
| Transport | MQTT 3.1.1 over TLS 8883, broker on the Brain host; no REST or MCP between layers |
| Units | water level **m**; air quality **ug/m3**; traffic speed **km/h**; power **kW** (Twin converts from % with `scale`) |
| Ids | `[a-z0-9][a-z0-9-]*`; the Twin lower-cases its ids (`WATER_07` -> `water-07`, `zone_a` -> `zone-a`), overrides in `integration.id_map` |
| Zones | `zone-a`, `zone-b`; the city-wide pseudo zone `city` is never exposed |
| Signature | envelope + HMAC-SHA256 over the body text; one key per pair: `twin-brain.key`, `twin-guardian.key`, `guardian-brain.key` |
| Versions | envelope `v: 1`; the Twin's native contract keeps `contract_version: "1.0"` (REST and stand-alone mode only) |
| Unit types | the Twin declares its civil-protection unit as `fire_truck` (the Brain's fire playbook); ambulances and police as is |
| Fail-closed | Brain without Guardian verdicts for 30 s: safe mode (risky actions need two sensor types; low-risk ones run). Brain without Twin: no commands |
| Ground truth | never on the bus: no `fault` field, no `city/faults`; `/truth` is test-key REST only |

## 5. Ports and start order

| Service | Port | Where |
|---|---|---|
| MQTT broker (TLS) | 8883 | Brain host |
| Brain timeline / graph API | 8090 / 8080 | Brain host, localhost |
| Twin REST (tests) | 8000 | Twin host |
| Twin stand-alone broker, our REST stand-ins | 1883, 8001, 8003, timeline 8002 | stand-alone mode only |
| Guardian | not in its spec (it mentions `GET /timeline`) | to confirm (R-G5) |

Order: 1. broker (Brain host) -> 2. Brain -> 3. Guardian -> 4. Twin (`twin/meta` retained first, then `twin/status online`, then readings). Check clocks (NTP) first: more than 10 s of skew means refusals. Restarts: the Twin's ids carry a run prefix, a restarted layer republishes its retained state.

## 6. Open points with the other teams

| # | With | Point |
|---|---|---|
| R-B1 | Brain | broker ACL: `twin` may read `guardian/quarantine/#` |
| R-B2 | Brain | no road-closure or public-alert playbook in `rules.yaml`; the Twin's `ROAD_01`, `ALERT_01/02` are not declared |
| R-B3 | Brain | no `rescue` unit type exists in the Twin: the flood playbook's dispatch finds no unit (*no free rescue*); name for civil protection to confirm |
| R-B4 | Brain | `ca.crt`, `twin` password, `twin-brain.key` privately; meta `alert` levels (water 1.3 m, air 150 are the Brain mock's values); id scheme for the Brain's scenarios (they use `zone-01`, `water-01`, `valve-01`) and a `twin/control` adapter |
| R-B5 | Brain | `traffic_density` (vehicles/km) and `traffic_flow` are not sent: confirm that is acceptable |
| R-G1 | Guardian | add `hmacs.twin` to `guardian/quarantine/#` |
| R-G2 | Guardian | who generates and shares `twin-guardian.key` |
| R-G3 | Guardian | confirm it ignores any `fault` field and never calls the Twin's REST `/truth` |
| R-G4 | Guardian | unknown sensor types and the 8-identical-readings rule |
| R-G5 | Guardian | its ports, its native v1.0 interface, and where its code is published for a joint test |
| Q1 to Q7 | all | see [`MATCHING.md`](MATCHING.md), section 7 |

Not yet validated in this repo: a run against the **real Guardian** (no code on hand), and a run through a real Mosquitto with the broker ACL (the tests used a Python broker that has no ACL).
