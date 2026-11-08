# Mocks

Stand-ins for the other layers, so the Brain can be built and tested alone. They speak the
real protocol (MQTT, sealed envelopes, topic names from `config/topics.yaml`), contain no Brain
logic, and use the same `src/common/` code as the Brain.

| Mock | Run | Config | Status |
|---|---|---|---|
| Twin | `python -m mocks.twin` | `config/twin-mock.yaml` | done |
| LLM | `python -m mocks.mock_llm [scripted\|slow\|offline\|invalid]` | `config/llm.yaml` (`mock`) | done |
| Guardian | `python -m mocks.guardian` | `config/guardian-mock.yaml` | done |

Scenarios that drive the three mocks (B1-B14, T5): `scenarios/README.md` at the repo root.

## Twin mock

A small deterministic city described in `mocks/city.yaml` (`sim.city`): zones, the sensors made for
each zone, actuators and response units. Nothing about the city is hard-coded in `mocks/twin/`.
Run it from the `brain/` folder with a broker and the keys of `README.md` in place. It needs
the `twin-brain` HMAC key and, for a TLS broker, the Twin's own broker account in
`secrets/twin-password.txt` (`auth` in `config/twin-mock.yaml`).

On connect it publishes `twin/status` (`online`, Last Will `offline`), the retained city
description `twin/meta`, and the retained state of every actuator and unit. Then, every
`sim.tick_s` seconds:

| Topic | Content |
|---|---|
| `city/{zone_id}/sensor/{type}/{sensor_id}` | one reading per sensor: `traffic_speed`, `traffic_density`, `traffic_flow`, `water_level`, `power_load`, `air_quality` (`seq` = tick, `origin: simulated`) |
| `city/{zone_id}/call` | emergency calls (`fire`, `accident` scenarios every tick, `flood` every 5 ticks from tick 3), in the zone of the scenario |
| `twin/actuator/{id}`, `twin/unit/{id}` | acknowledgement of a command, and unit status changes (`en_route` → `on_scene` → `returning` → `free`) |

Timestamps are wall-clock (`sim.clock: wall`, the default; the Brain refuses anything older than
10 s), also in manual mode, so stepping by hand never makes readings stale. `sim.clock: simulated`
(`epoch + tick * tick_s`) is repeatable and meant for deterministic tests only. `sim.run_id: auto`
keeps ids unique across restarts, so the Brain's replay check never fires by accident.

**The city** (`mocks/city.yaml`): six zones (`zone-01` port, `zone-02` docks, `zone-03` centre, `zone-04`
hospital zone, `zone-05` industrial with the substation, `zone-06` residential), 6 sensors per zone
(`traffic-speed-NN`, `traffic-density-NN`, `traffic-flow-NN`, `water-NN`, `power-NN`, `air-NN`, NN =
zone number), plus a fire signature in zones 05 and 06 (`smoke-NN` %/ft, `co-NN` ppm, `co2-NN` ppm,
`temp-NN` C, with an absolute noise floor so a calm value is never flat): 44 sensors, 3 valves, 7 traffic signals, 2 grid switches, 2 ambulances, 2 fire trucks, 1 police, 1
rescue unit. A command changes the zones its actuator `controls` (`grid-01` also feeds `zone-06`); a
unit works in the zone it was sent to (`dispatch` `zone_id`, default its home zone) and returns home.

**Scenarios** (`sim.scenario` in `sim.scenario_zone`, started once, on the first tick at or after `scenario_after_ticks`, in auto and manual mode;
a `trigger` control message (`scenario`, optional `zone`) starts one at once and cancels the configured start;
a zone has one scenario at a time, zones run theirs side by side): `calm`, `flood` (water over
1.3 m, rising), `fire` (particulates up + fire calls; where the zone has the fire-signature
sensors, a held-out NIST flaming foam test is replayed: smoke, CO, CO2 and heat rise),
`accident` (traffic drops + calls), `traffic-jam`, `cooking` (burnt toast: particulates up as much as
a fire, a held-out NIST toast test replayed: smoke up, no CO, no heat, no call). Traces:
`twin/traces/*.csv`, written by `tools/build_cases.py`. Effects are small scripted perturbations, not city physics. A target has one
effect at a time: a new command replaces it, `close` / `normal` or a unit being `free` again ends it.

**Commands** (provisional, until the output module fixes the format). Topic
`brain/cmd/{actuator_id or unit_id}`, envelope sealed with the twin-brain key (`hmac` or
`hmacs.twin`), body:

```json
{"id": "brain-000042", "timestamp": "2026-11-14T10:22:04.000Z", "source": "brain",
 "target_id": "valve-01", "action": "open", "parameters": {"opening": 0.8}}
```

| Target | Actions | Parameters |
|---|---|---|
| `valve-01`..`valve-03` | `open`, `close` | `opening` 0..1 (default 1); lowers the water level of the zone by up to 0.3 m |
| `signal-01`..`signal-07` | `normal`, `priority` | `priority` adds speed and flow in the zone |
| `grid-01`, `grid-02` | `normal`, `shed` | `shed` cuts power load by 15 % in the zones the switch controls |
| `unit-ambulance-01`/`02`, `unit-fire-01`/`02`, `unit-police-01`, `unit-rescue-01` | `dispatch`, `recall` | `zone_id` (default the home zone); a fire truck on scene cuts particulates by 30 %, the others improve traffic |

The Twin acknowledges after `sim.command_delay_ticks` with `correlation_id` = the command `id`
and `result` `done`, `rejected` or `failed` (+ `reason`, `state`). Same `id` again returns the
same acknowledgement and is never applied twice; same `id` with another payload is `rejected`
(`command_id_reused`).
A unit is reserved when its `dispatch` is accepted, so a second `dispatch` before the first is
applied, or while the unit is out, is `rejected` (`unit_busy`). `recall` of a unit already
`returning` is acknowledged `done` and changes nothing (the trip is not extended); `recall` of a
unit that is `free` is `rejected` (`unit_not_deployed`). Call ids are `call-{run_id}-{tick}`, unique
across resets and restarts. Commands that fail the HMAC, are older than `max_command_age_s`, or name
an unknown target get no answer (logged as `message_refused`).

**Faults** (`faults:` in `config/twin-mock.yaml`, or a `faults` control message). Each reading
fault is meant to hit one check of the input module (`tests/mocks/test_twin_service.py`,
`test_readme_fault_table`, runs every row through the real `InputService`). `seq` is the tick,
unless a `late` reading took a number meanwhile; it always increases per sensor:

| Fault | Brain answer |
|---|---|
| `malformed`, `wrong_type`, `wrong_unit` | `E_FORMAT` |
| `bad_hmac` | `E_AUTH` |
| `old` with `delay_ticks` × `tick_s` over 10 s (`clock_window_s`) | `E_STALE` |
| `old` younger than that (default `delay_ticks: 2`), `delay`, `duplicate`, `replay` | `E_REPLAY`: the reading is stamped in the past and keeps its old `seq`, so it is behind a newer one |
| `unknown_id` | `E_UNKNOWN_ID` |
| `impossible` | `E_RANGE` |
| `falsify` (valid but false value) | accepted; Guardian's job |
| `missing`, `freeze` | accepted (a gap, stuck data) |
| `late` (`delay_ticks`) | accepted: delivered `delay_ticks` later, its `seq` allocated on delivery, so it is late but in order |
| `reject`, `failure`, `lost_ack`, `no_effect` | actuator/unit: `rejected`, `failed`, no acknowledgement until the Brain retries the same id, `done` but no effect on later readings |

**Rush hour** (`rush_hour: {start_tick, end_tick}`): in every zone, speed falls by up to 40 %, density
triples and flow rises by 80 % in a smooth peak in the middle, and never reaches an alert, so the
Brain must see a busy but normal city (scenario B1). A scenario in a zone still overrides it.

**Calls with free text.** A scenario's calls take their text from `call_texts` (in turn, `{zone}` is
replaced), else `<category> reported in <zone>`. `calls:` scripts calls at given ticks, published
exactly as written: a prompt injection (B10), ten fake fire calls (B7, `count: 10`), several
incidents at once (B11). The `call` control message (`zone_id`, `category`, `text`) sends one at run
time. Categories are those of the Brain's call model: `fire`, `accident`, `flood`, `medical`, `other`.

**Actuator modes** (`actuator_modes:` in `config/twin-mock.yaml`, or `actuator_modes` in a `faults`
control message), per actuator or unit; the default is `ok`:

| Mode | Behaviour |
|---|---|
| `ok` | applied and acknowledged `done` after `sim.command_delay_ticks`; later readings change |
| `slow` | applied and acknowledged after `sim.slow_ack_s` (5 s) |
| `no_ack` | applied, never acknowledged (not even when the Brain retries the same id) |
| `stuck` | acknowledged `done`, nothing changes (alias of the `no_effect` fault) |
| `reject_quarantined` | a quarantined device refuses commands with `reason: quarantined` (without this mode the reason is `device_unavailable`) |

A command with an `id` already executed is always answered with the same acknowledgement, never applied twice. The four
non-default modes also exist as timed faults on one device (`faults:`).

**Control** (test only). Topic `twin/control`, sealed with the twin-brain key, body
`{"op": ...}`: `trigger` (`scenario`, optional `zone`), `call` (`zone_id`, `category`, `text`), `faults` (`faults: [...]` and / or `actuator_modes: {id: mode}`), `contain` (`device_id`,
`action`: `isolate` / `quarantine` / `release`, takes effect next tick), `reset` (optional `seed`, `scenario`, `scenario_zone`, `scenario_after_ticks`,
`rush_hour`, `calls`: the same seed and scenario
give the same readings, so a scenario runner starts each run with one message; new run id,
goes `offline` then `online` so the Brain restarts its `seq` counters; the values stay until a
later reset names them, so the runner always sends all six), `step` (use with
`sim.mode: manual`), `offline` (the Twin goes down: `twin/status` offline, then no readings, calls
or acknowledgements and no answer to commands, the city is frozen; scenario B13) and `online`
(back: status, city description and states again, the next tick continues).

**Tests:** `pytest tests/mocks`. They include the mock feeding the Brain's real input module:
every message must pass, and each fault must give the code above.

## Guardian mock

Stands in for the Guardian layer (R1). Run it from the `brain/` folder with a broker and the keys of
`README.md` in place: it needs the `guardian-brain` HMAC key and, for a TLS broker, the Guardian's
own broker account in `secrets/guardian-password.txt` (`auth` in `config/guardian-mock.yaml`).

| Direction | Topic | Content |
|---|---|---|
| in | `city/+/sensor/+/+` | Twin readings. They are sealed for the Brain with the twin-brain key; the mock reads the body and, when it has that key, drops readings whose HMAC is wrong (`verify_twin_hmac`) |
| in | `twin/meta` | city description (until it arrives: `mocks/city.yaml`) |
| in | `guardian/control` | test control, sealed with the guardian-brain key |
| out | `guardian/verdict/{sensor_id}` (retained) | `id`, `timestamp`, `source: guardian`, `correlation_id` = the reading id, `sensor_id`, `verdict` `trusted` / `suspicious` / `rejected`, `reason`, `trust_score` 0..1, `details` |
| out | `guardian/quarantine/{device_id}` (retained) | `device_id`, `state` `quarantined` / `released`, `reason` |
| out | `guardian/status` (retained, Last Will `offline`) | `online` / `offline` |

Everything goes out sealed with the guardian-brain key (`envelope: hmac`, or `hmacs` for
`{"hmacs": {"brain": ...}}`), topic names from `config/topics.yaml` (`external_in`, source
`guardian`). `tests/mocks/test_guardian.py` feeds every message to the Brain's real input module:
all pass, with no special case.

**Checks** (mode `auto`, the default), on every reading, first match wins:

| Check | Verdict, reason |
|---|---|
| value outside the sensor's `[min, max]` | `rejected`, `out_of_range` |
| change since the last trusted value above `checks.max_step` for the type (per tick; `{rise, fall}` allowed) | `rejected`, `implausible_jump` |
| same value `frozen_rejected_n` (10) readings in a row | `rejected`, `frozen` |
| pinned at the min or max bound `flatline_n` (5) readings | `rejected`, `flatline` |
| same value `frozen_suspicious_n` (5) readings in a row | `suspicious`, `frozen` |
| otherwise | `trusted`, `consistent` (score 0.95, 0.90 when far from the sensor's own baseline) |

A jump stays rejected while the value stays away from the last trusted level; a real step is
accepted once it held `rebase_n` readings with live noise (a forged constant never is). The
limits let the Twin mock's own scenario steps through (flood 0.4 -> 1.4 m, fire, accident) and catch
the fake flood of T5 (0.4 -> 1.8 m) at its first reading. With `isolate_on_reject`, a sensor rejected
for `implausible_jump` is also isolated (quarantine notice, released after `auto_release_after`
trusted readings). Repeated reading ids, older `seq`, unknown sensors and non-numbers are ignored.

A verdict that changes is published at once; an unchanged one is published again every `refresh_s`
(5 s), so the Brain (offline after 30 s without a verdict) never thinks Guardian is silent.

**Modes** (`modes:` in the config, or `{"op": "mode", "mode": ...}` / `"modes": [...]` on
`guardian/control`; `{"op": "reset"}` releases every quarantine, forgets the history and goes back to
the config, or to the `modes` it carries):

| Mode | Behaviour |
|---|---|
| `auto` | the checks above; also clears forced modes and `slow` |
| `trust_all` | every sensor trusted, score 0.95 |
| `reject:<sensor_id>` | rejected, reason `out_of_range` |
| `suspicious:<sensor_id>` | suspicious, score 0.5 |
| `flip_flop[:<sensor_id>]` | alternates rejected / trusted on every reading (one sensor or all) |
| `clear:<sensor_id>` | back to the checks for that sensor |
| `quarantine:<device_id>` / `release:<device_id>` | quarantine notice for any sensor, actuator or unit |
| `slow` / `fast` | verdicts `slow_s` (5 s) late, stamped when they leave / back to normal |
| `offline` / `online` | status offline, then silent (the Brain goes to safe mode) / online and every verdict again |

## Mock LLM

`python -m mocks.mock_llm [scripted|slow|offline|invalid]`: the real LLM service (`src/llm/`) with a
scripted backend, so other modules can be tested without the edge device or a GPU. Same topics and
timeline events as the real module; settings in `config/llm.yaml` (`mock`). Details: `brain/README.md`,
LLM module.

The mock (only) also listens on `brain/control/llm` (`config/topics.yaml`, `control.llm`), plain JSON
`{"mode": "offline"}`, so the scenario runner can switch its mode during a run (B12).
