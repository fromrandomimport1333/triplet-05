# Stand-ins for the Guardian and the City Brain (rule R1)

Written by the **Twin team** so the Twin can be run, tested and demonstrated alone. They are deliberately simple and explainable, and they are **not** the real layers (those belong to the Guardian and Brain teams, in `guardian/` and `brain/`). What they do follow exactly is the **contract** the real layers must follow ([`integration/TWIN-INTEGRATION.md`](../../integration/TWIN-INTEGRATION.md), [`docs/schemas/`](../../docs/schemas/)), so swapping a stand-in for a real layer needs no code change, only a URL or key in config.

```
Twin --MQTT readings--> Guardian stand-in --guardian/verdicts, POST /verify--> Brain stand-in
  ^                         |  (quarantine, Guardian key)                          |
  +-------------------------+------ POST /actuators/<id> + verdict_ref, Brain key -+
```

## Run (repository root, virtual environment active, Mosquitto and the Twin running)

```powershell
python twin\mocks\guardian_mock.py          # REST http://127.0.0.1:8001
python twin\mocks\brain_mock.py             # REST http://127.0.0.1:8003
python twin\tools\contract_check.py --provoke --seconds 40     # check that both speak the contract
```

Settings: `mocks.yaml` (thresholds, ports, keys, tiers). The broker address and the signing key come from `../city.yaml`. Environment variables override both (`.env.example`).

## Guardian stand-in

| Contract item | What the stand-in does |
|---|---|
| `POST /verify` | Body `{"sensor_ids": [...]}`, `{"reading_ids": ["WATER_08#42"]}` or `{"readings": [...]}`, validated against `verify_request.schema.json`. Answers one `verdict`: worst `status` (`trusted` / `suspicious` / `untrusted`), lowest `trust` (0 to 1), `reasons` prefixed with the sensor id, a `verdict_id` for the Brain's `verdict_ref` |
| `guardian/verdicts/<sensor_id>` | A signed verdict each time a sensor's status or main reason changes |
| `guardian/verify`, `guardian/alerts` | Each `/verify` answer; alerts `quarantined`, `released`, `watcher`, ... |
| `GET /verdict/{sensor_id \| reading_id \| verdict_id}`, `GET /verdicts`, `GET /devices/{id}/status`, `POST /devices/{id}/release`, `GET /alerts`, `GET /health` | As in the contract |

Checks, any failure = `untrusted`: signature (checked **first**, so a bad message changes no stored state); replay (`seq` not increasing); timestamp more than 5 s old or in the future; more than 3 messages per second (network flooding); missing or impossible value; jump too big for one step; 10 identical values (stuck); 3 readings in a row away from trusted sensors of the same kind in the zone; no data for 5 s. One or two readings away from the peers = `suspicious`.

Graded trust: 1.0 trusted, 0.65 suspicious, 0.1 untrusted on a rule, 0.0 on an integrity failure. The real Guardian is expected to compute the Phase 0 score `s = 1 - (0.25*aIF + 0.35*aLOF + 0.40*(1 - c))`; the stand-in only imitates its output.

Containment: after 3 untrusted 5-second windows in a row, it calls the Twin's `POST /quarantine/<id>` with the **Guardian key**; after 3 clean windows it releases. If more than 30 % of sensors are untrusted at once, it raises a `watcher` alert and does not quarantine. Forged or replayed messages are reported but do not count toward a genuine sensor's quarantine. It **ignores** the `fault` label and `city/faults`.

Not done by the stand-in: machine learning, corroboration with rain, traffic or calls, actuator-effect checks.

## Brain stand-in

| Contract item | What the stand-in does |
|---|---|
| Ask before acting | `POST /verify` with the sensors behind the decision, before **every** actuator call |
| Tiered gate (Phase 0 report, Table 5) | Tier B (valve, road, signal, grid): trusted = execute; suspicious = hold 5 s, ask once more; untrusted = block; Guardian unreachable = block (fail closed). Tier A (alert, dispatch): trusted = execute; suspicious = execute and `operator_flag`; untrusted = block and `operator_alert`; Guardian unreachable = `operator_request` (never dropped) |
| `verdict_ref` | Every command carries the `verdict_id` it relied on, or `operator:<request id>` after an operator approval |
| `brain/decisions` | Every step, signed: `flood_detected`, `jam_detected`, `verification`, `action_held`, `action_blocked`, `operator_flag`, `operator_alert`, `operator_request`, `command_sent`, `actuator_result`, `action_failed`, `fallback`, `flood_cleared`, `jam_cleared`, `command_failed` |
| Done only when confirmed | An action counts only when the Twin's feedback says `ok: true` within 10 s; otherwise the next action is tried |

Rules (lower thresholds than the report's Table 4, so a demo takes minutes): **flood** = water at or above 1.0 m for 3 readings; **jam** = half of a zone's cameras below 10 km/h and above 80 % density for 30 s.

Playbooks: **flood** = open the valve serving the sensor, or if it fails, close the zone's road and dispatch civil protection; red public alert; when the level is back below 0.7 m for 5 readings, close the valve and reopen the road. **Jam** = the zone's traffic signal to phase 2, and an information alert.

Degraded mode: if the Guardian is down it keeps detecting and logging, blocks tier B and sends tier A to the operator; if the Twin is down it logs `command_failed` and keeps running. Readings marked `quarantined` are ignored (isolated input).

Operator API (port 8003): `GET /operator/requests`, `POST /operator/requests/<id>` with `{"approve": true}` or `false`; also `GET /decisions`, `GET /health`.

## Logs

Both write to the root `logs/` folder: `guardian_verdicts.jsonl`, `guardian_alerts.jsonl`, `brain_decisions.jsonl` (one JSON per line).

## Stand-ins for the real partner protocol (`partner_mock.py`)

The REST stand-ins above follow the **Twin team's** contract (`POST /verify`, `brain/decisions`, `verdict_ref`). The real City Brain and Guardian do **not** speak it: they use MQTT over TLS with signed envelopes (see [../INTERFACE.md](../INTERFACE.md) and [../../integration/MATCHING.md](../../integration/MATCHING.md)). `partner_mock.py` imitates the real formats, so the Twin's integration profile can be shown without the partners' code:

```
python twin/mocks/partner_mock.py guardian | brain | both
```

- **Guardian** (interface v1.3): reads `twin/meta` and `city/+/sensor/+/+` (checks `hmacs.guardian` with `twin-guardian.key`), publishes `guardian/verdict/{sensor_id}` (retained; `trusted` / `suspicious` / `rejected`, `reason`, `trust_score`, `correlation_id` = the reading's `id`), `guardian/quarantine/{device_id}` after 3 consecutive rejections (sealed for the Twin and the Brain), `guardian/status` with a Last Will, and a refresh of every verdict after 20 s of silence. Checks: range from the meta, increasing `seq`, repeated `id`, implausible jump, 8 identical values.
- **Brain**: a flood playbook only. A trusted `water_level` at or above its `alert` three times in a row sends `brain/cmd/{valve}` `open` (sealed for Twin and Guardian) and reads the acknowledgement on `twin/actuator/{id}`.

Settings: `wire:` in `mocks.yaml` (broker, accounts, key files, thresholds), environment overrides listed in the file's docstring. The old REST stand-ins keep serving the Twin's stand-alone mode and scenarios T1 to T12.
