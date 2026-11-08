# Twin server (SUMO bridge + REST API + MQTT)

One process, `src/twin.py`. Sensors and actuators are defined in `city.yaml`; nothing about places is hard-coded.

```
SUMO / Python models -> signed JSON readings -> MQTT  city/<zone>/<kind>/<sensor_id>
Brain -> POST /actuators/<id> -> applied between SUMO steps -> feedback on MQTT city/<zone>/actuator/<id>/state
Test scripts -> POST /faults -> a reading is stuck / spoofed / dropped (the true value is kept for scoring)
Fault start and stop -> fault_event on MQTT city/faults
```

## Two ways to run

| Mode | Partners | Protocol | Switch |
|---|---|---|---|
| **Alone** (default) | our stand-ins in `mocks/` | REST on port 8000 + plaintext MQTT on 1883, in-message `sig` | `integration.enabled: false` |
| **With the real Brain and Guardian** | the partners' layers | MQTT over TLS (8883), signed envelopes, partner topics | `integration.enabled: true` or `INTEGRATION_ENABLED=1` |

The Twin core is the same in both. REST stays on in both modes (tests and scenarios drive the Twin through it). Contract: [INTERFACE.md](INTERFACE.md); how the three layers fit together: [../integration/TWIN-INTEGRATION.md](../integration/TWIN-INTEGRATION.md); what had to change and why: [../integration/MATCHING.md](../integration/MATCHING.md).

## Run alone, with its mocks

```
pip install -r requirements.txt
python twin/src/twin.py --fake            # needs an MQTT broker on 127.0.0.1:1883 (Mosquitto)
python twin/mocks/guardian_mock.py        # REST stand-in on :8001
python twin/mocks/brain_mock.py           # REST stand-in on :8003
python twin/tools/contract_check.py --provoke --seconds 40
```

Or `.\start.ps1` / `docker compose -f docker-compose.twin.yml up --build` from the repository root. Tests: `python -m pytest twin/tests scenarios/tests`.

To see the Twin speak the real partner protocol without the partners' code, point it at any TLS broker that has the three accounts and key files, then run the stand-ins that follow the real formats (`mocks/partner_mock.py`, see [mocks/README.md](mocks/README.md)):

```
python twin/src/twin.py --fake            # with integration.enabled: true and the secrets configured below
python twin/mocks/partner_mock.py both    # Guardian (verdicts, quarantine) and Brain (flood playbook) over MQTT/TLS
```

## Run with the real Brain and the real Guardian

1. **Get the secrets privately** from the Brain team: `ca.crt`, the `twin` password, `twin-brain.key`; from the Guardian team (or agree together): `twin-guardian.key`. Put them in a git-ignored `secrets/` folder at the repository root (`.gitignore` already excludes `secrets/`, `*.key`, `ca.crt`).
2. **Start order:** broker (Brain host) -> Brain -> Guardian -> Twin. The Twin publishes `twin/meta` first, so the other two learn the city from it; a restarted Twin gets fresh ids and the Brain resets its cooldowns when the status goes `offline` then `online`.
3. **Point the Twin at them** (nothing to edit in code):

```
set INTEGRATION_ENABLED=1
set PARTNER_MQTT_HOST=192.168.1.20            # the Brain laptop
set PARTNER_MQTT_CA_CERT=secrets\ca.crt
set PARTNER_MQTT_PASSWORD_FILE=secrets\twin-password.txt
set TWIN_BRAIN_KEY_FILE=secrets\keys\twin-brain.key
set TWIN_GUARDIAN_KEY_FILE=secrets\keys\twin-guardian.key
python twin/src/twin.py --fake                # or without --fake for SUMO
```

4. **Check:** the Twin prints `[bridge] connected`; the Brain timeline shows the Twin pill green and no `brain/error`; `[bridge] command ...` lines appear when the Brain acts. The City Brain team's `tools/partner_check.py` also tests TLS, account, key and clock.
5. **Drive a scenario** through REST as before (`POST /scenario/conditions`, `/faults`, `/emergency_calls`, test key). The Brain's own scenarios (`python scenarios/run.py B2`) use its mock-city ids and its `twin/control` messages, which the Twin does not implement yet (MATCHING.md Q1).

## Integration settings (`integration:` in `city.yaml`)

| Key | Meaning | Environment variable |
|---|---|---|
| `enabled` | partner profile on/off | `INTEGRATION_ENABLED` |
| `disable_native_mqtt` | when on, do not also publish native messages on the local broker | |
| `mqtt.host`, `port`, `tls`, `ca_cert`, `username`, `password_file`, `client_id`, `keepalive_s` | the partners' broker and our account | `PARTNER_MQTT_HOST`, `PARTNER_MQTT_PORT`, `PARTNER_MQTT_TLS`, `PARTNER_MQTT_CA_CERT`, `PARTNER_MQTT_USERNAME`, `PARTNER_MQTT_PASSWORD_FILE` |
| `keys.brain`, `keys.guardian` | HMAC key files | `TWIN_BRAIN_KEY_FILE`, `TWIN_GUARDIAN_KEY_FILE` |
| `clock_window_s`, `qos_readings`, `run_id` | accepted skew on commands (10), readings QoS (0), id prefix (empty = from the start time) | |
| `id_map`, `zones` | names the partners see; zone type, exposure, adjacency | |
| `sensors`, `actuators`, `unit_types`, `call_categories` | what is declared in `twin/meta` and how values and actions are translated | |

Placeholders to confirm with the partners: `power_load.scale: 10.0` (assumes a 1000 kW feeder), the `alert` levels (water 1.3 m, air 150), `traffic_signal` phase map, `unit_types: {civil_protection: fire_truck}`.

## Integration adapters

Added so the Twin matches the real Brain and Guardian **without rewriting the core** (`src/twin.py` gained only a hook in `publish`, a tick, an optional `unit_id` for `dispatch`, a `recall_unit` method and `command_problem`).

| File | What it does | Why |
|---|---|---|
| `src/protocol.py` | Envelope seal/verify (`hmac`/`hmacs`, exact body text), id normalisation, freshness check, typed `ProtocolError` (`E_FORMAT`, `E_AUTH`, `E_STALE`) | The partners accept only this envelope; the Twin's own `sig` scheme is not understood by them. Pure functions, unit-tested against the published test vector |
| `src/translate.py` | City map from `city.yaml` + `integration:`: builds `twin/meta`, turns native readings, calls and feedback into partner bodies, turns Brain commands into native commands; whitelists fields so ground truth cannot leak | The partners need the city declared first, other topics, lower-case ids, other units, other type names |
| `src/bridge.py` | MQTT/TLS client: connect, Last Will, `meta` then `status`, `brain/cmd/+` intake (verify, once-per-id, ack within 4 s), quarantine intake, unit status, `brain/error` logging | The Twin had no MQTT command input; the Brain never calls REST |
| `mocks/partner_mock.py` | Guardian and Brain stand-ins speaking the real formats | Our REST stand-ins describe a protocol neither real layer implements; the old ones stay for stand-alone mode |

The logic of the city (SUMO, models, faults, scoring truth) knows nothing of the partners' protocol; the bridge is the only code that does. Tests: `tests/test_integration.py` (broker-less, 16 tests).

## Run alone on Windows (details, SUMO, ESP32)

1. `pip install -r requirements.txt`
2. An MQTT broker must be running. If Mosquitto is already a Windows service (check Services), do nothing; otherwise install it from https://mosquitto.org/download/ and start it. To let ESP32 boards connect, see "Letting ESP32 connect" below.
3. **Test without SUMO first:** `python src/twin.py --fake`, then open http://localhost:8000/docs
4. In a second terminal: `python src/listen.py` to watch every message.
5. **With SUMO:** edit `city.yaml`: set `sumo.cfg` to your generated `osm.sumocfg`, and replace the placeholder `lonlat` values with places inside your scenario. Then run `python src/twin.py`. Each device appears as a coloured marker in `sumo-gui`.

Tested in `--fake` mode and against a real SUMO scenario. Test results and known bugs are in the [main README](../README.md).

## Quick API tour

Keys (header `X-API-Key`): actuators use the Brain key `change-me`; faults, `/scenario/...`, `/truth`, `/events` and new emergency calls use the test key `test-key`; quarantine uses the Guardian key `guardian-twin-key`. `GET /contract` describes everything.

The full contract (every topic, field, actuator and error) is in [INTERFACE.md](INTERFACE.md).

| Call | What it does |
|---|---|
| `GET /devices`, `GET /sensors/{id}`, `GET /tools` | list devices, latest reading, tool names |
| `POST /actuators/VALVE_03` `{"cmd_id":"c1","args":{"state":"open"}}` | valve (`open`/`closed`) |
| `POST /actuators/TL_01` `{"cmd_id":"c2","args":{"phase":2}}` | traffic light phase |
| `POST /actuators/ROAD_01` `{"cmd_id":"c3","args":{"state":"closed"}}` | close a street (`closed`/`open`) |
| `POST /actuators/ALERT_01` `{"cmd_id":"c4","args":{"level":"red","msg":"flood"}}` | public alert; level `info`/`yellow`/`orange`/`red`, text 1 to 280 characters |
| `POST /actuators/GRID_A` `{"cmd_id":"c5","args":{"action":"shed"}}` | power grid `normal`/`reroute`/`shed`: changes that zone's power load |
| `POST /tools/dispatch_emergency` `{"cmd_id":"c6","args":{"type":"ambulance","zone_id":"zone_a"}}` | send a unit; feedback `dispatched` (with ETA) then `arrived` |
| `POST /tools/set_water_valve` `{"cmd_id":"c7","args":{"zone_id":"zone_a","state":"open"}}` | the same actuators, called by Phase 0 tool name and zone |
| `POST /quarantine/AIR_01`, `DELETE /quarantine/AIR_01` | quarantine / release a sensor; **Guardian key only** (`api.guardian_key`) |
| `POST /scenario/rain/60` | start 60 mm/h rain everywhere (water sensors rise) |
| `POST /scenario/conditions` `{"zone_id":"zone_a","jam":1,"pollution":8,"power_demand":1.7,"rain_mm_h":30}` | jams, smog, power demand and rain per zone, for scenarios |
| `POST /scenario/reset` | calm weather, no jam, grid normal, faults cleared |
| `POST /faults` `{"sensor_id":"WATER_07","kind":"spoof","value":4.2,"duration_s":60}` | fault. Kinds: `stuck`, `dropout`, `spoof`, `spike`, `drift`, `replay`, `flood_network`, `actuator_stuck` |
| `GET /truth/{id}` | ground truth, for scoring only (the Brain and Guardian must never call it) |
| `DELETE /faults/{id}`, `GET /faults` | end a fault early; list active faults |
| `GET /events` | recent events from the JSON logs (`logs/twin_events*.json`). Keep `limit` low: large responses fail on the development computer |
| `POST /emergency_calls` `{"call_type":"fire","lonlat":[10.18,36.80],"severity":"high"}` | send an emergency call |
| `GET /emergency_calls` | saved emergency calls from `logs/emergency_calls.json`, newest first |

Run the unit tests (no broker or SUMO needed): `python -m pytest twin/tests` from the repository root.

## Fault labels

While a fault changes a message, that message carries a `fault` field inside the signed part. Readings and feedback without a fault do not have it. This applies to readings during `spoof`, `spike`, `stuck`, `drift` and `flood_network`, and to actuator feedback during `actuator_stuck`. A `replay` re-sends old messages unchanged, so they carry no label.

```json
"fault": {"kind": "spoof", "id": "F-cef3d49dcf59"}
```

`POST /faults` returns the same ID as `fault_id`. A `dropout` publishes no readings, so its start and stop times mark the gap.

Each start and stop also publishes a signed `fault_event` on MQTT topic `city/faults`, and logs it:

| Field | Meaning |
|---|---|
| `type`, `event` | `fault_event`; `start` or `stop` |
| `fault_id`, `sensor_id`, `zone_id`, `kind`, `value` | Which fault, on which device |
| `start_sim_time`, `start_utc` | When it started |
| `end_sim_time`, `end_utc` | When it stopped; `null` in a `start` event |
| `planned_end_sim_time` | When it was due to end; `null` if it had no `duration_s` |
| `reason` | `expired`, `cleared`, `replaced` or `twin_stopped`; `null` in a `start` event |
| `contract_version`, `ts`, `sig` | As in other messages |

A forced stop leaves a `start` with no `stop`. The label reveals a fault to anything reading the stream, so a Guardian being tested must ignore it.

## Traffic speeds

`mean_speed_kmh` on cameras, and `value` on `traffic` sensors, are capped at the edge's speed limit (`traci.lane.getMaxSpeed`). An empty edge reports the limit, so use a camera's `value` (vehicle count) to tell empty from free-flowing. In `--fake` mode, camera speed, stopped vehicles and density are simulated consistently from the vehicle count (road capacity 30 vehicles, limit 50 km/h), and `traffic` values stop at 50 km/h. Camera faults change all of these fields together, so a fake count never contradicts the speed next to it.

Name each road with `edge:` in `city.yaml`. Without it, the nearest road within 200 m is used, which can be the wrong one. Startup prints a `[road]` line for each traffic sensor, and a `[warn]` line for a doubtful match. A sensor with no road publishes nothing.

## Logs

Files are written to `logs/` (setting `logs.dir`):

| File | Contents | Rotated? |
|---|---|---|
| `twin_events.json` | Readings, feedback and fault events | At `logs.rotate_mb` (20 MB), keeping `logs.keep_files` (5) files counting the active one |
| `twin_events-<UTC time>.json` | Older, full copies | Oldest deleted first |
| `fault_events.json` | Fault start and stop events | No |
| `emergency_calls.json` | Emergency calls | No |

`twin_events.json`, `emergency_calls.json` and `twin_events.db` in this folder itself are older data, and are no longer used.

## Real ESP32 devices

Set `source: esp32` for that device in `city.yaml`. The twin then stops generating its readings; the ESP32 publishes to the same topic with the same JSON fields (`contract_version`, `type`, `sensor_id`, `zone_id`, `kind`, `value`, `unit`, `ts`, `seq`, `sig`).

## Before real use

Change `api_key` and `hmac_key` in `city.yaml`. The simulated models (water, rain, air, power) are simple placeholders: tune them to your scenarios.

## Letting ESP32 connect (Mosquitto 2.x)

By default Mosquitto 2.x accepts only local connections. Add to `mosquitto.conf` (in the Mosquitto install folder), then restart the service:

```
listener 1883 0.0.0.0
allow_anonymous false
password_file C:\Program Files\mosquitto\passwd
```

Create the logins (run in the install folder, `-c` only the first time): `mosquitto_passwd -c passwd twin`, then `mosquitto_passwd passwd esp32`. Put the same `username`/`password` in `city.yaml` under `mqtt:` for the twin, and in each ESP32 sketch. Allow port 1883 in the Windows firewall for the private network only.
