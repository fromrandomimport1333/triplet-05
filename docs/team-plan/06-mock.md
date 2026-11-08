# Team 6: Mock

**People:** 1 · **Code:** `brain/mocks/`, `brain/scenarios/` · **Read first:** [`00-overview-and-contracts.md`](00-overview-and-contracts.md), [`../city-brain-interface-spec.pdf`](../city-brain-interface-spec.pdf) sections 5–7

## 1. Mission

You build the **fake city** the City Brain is tested against. Your mocks stand in for the Twin and Guardian until the real layers are connected, and they make every test scenario **repeatable with one command**. Phase 1 requirement R1 says our layer must run alone with mocks of the other two: **that's your work.**

**Every other team depends on you from week 1.** Ship a simple version first, improve it after.

## 2. You own / you don't own

| You own | You don't own |
|---|---|
| Mock Twin (city description, sensors, calls, unit status) | Mock LLM (LLM team) |
| Mock Guardian (verdicts, quarantine, status) | The real Twin and Guardian |
| Mock actuators (receive commands, acknowledge, change readings) | |
| Scenario files and the runner `./scenarios/run <id>` | |

## 3. What your mocks must look exactly like

Your mocks use **exactly the interface spec**: same topics, same envelope `{"v":1,"body":"…","hmac":"…"}`, same HMAC with the **test keys**, same fields, timestamps and unique ids. If the Brain works with your mocks, it must work with the real layers.

| Mock | Publishes | Subscribes |
|---|---|---|
| **Mock Twin** | `twin/meta` (retained), `city/{zone}/sensor/{type}/{id}`, `city/{zone}/call`, `twin/unit/{id}`, `twin/status` (Last Will) | — |
| **Mock actuators** (part of mock Twin) | `twin/actuator/{id}` (acknowledgements, state) | `brain/cmd/#` |
| **Mock Guardian** | `guardian/verdict/{sensor_id}` (retained), `guardian/quarantine/{device_id}` (retained), `guardian/status` (Last Will) | `city/#` (to give verdicts on readings) |

## 4. Mock city

A small city in `mocks/city.yaml` matching the Phase 0 sensor and actuator list:

| Zones | Sensors | Actuators | Units |
|---|---|---|---|
| 5–6 zones: port, docks, centre, hospital zone, industrial (substation), residential | Per zone: traffic (speed, density), water level, power load, air quality | Valves (2–3), traffic signals (1–2 per zone), grid switches (1–2) | 2 ambulances, 2 fire units, 1 police, 1 rescue |

Readings follow a realistic baseline + **noise** (real sensors are noisy), a daily pattern for traffic (rush hour), and react to commands.

## 5. Mock behaviour

### Mock actuators
| Mode | Behaviour |
|---|---|
| `ok` | Acknowledge `done` within 0.5 s and **change later readings** (valve opened → water level falls) |
| `slow` | Acknowledge after 5 s |
| `no_ack` | Never acknowledge |
| `stuck` | Acknowledge `done` but nothing changes in the readings |
| `reject_quarantined` | Refuse commands to quarantined devices with `reason: quarantined` |
| — | Always ignore a command whose `id` was already executed |

### Mock Guardian
| Mode | Behaviour |
|---|---|
| `trust_all` | Every sensor trusted (trust score 0.95) |
| `reject:<sensor_id>` | That sensor rejected, `reason: out_of_range` |
| `suspicious:<sensor_id>` | That sensor suspicious (trust score 0.5) |
| `quarantine:<device_id>` | Quarantine notice for that device |
| `slow` | Verdicts 5 s late |
| `offline` | Disconnects: `guardian/status = offline` |
| `flip_flop` | Alternates trusted / rejected (robustness test) |

### Fault injection in the mock Twin
Bad messages to test the input team: wrong HMAC, old timestamp, repeated id, missing field, unknown sensor, value out of range.

## 6. Scenarios

One YAML file per scenario in `scenarios/`, describing: city, timeline of readings / calls / faults, Guardian mode, actuator mode, **expected result**. Run with `./scenarios/run <id>` → starts the mocks, plays the scenario, saves the log, prints **PASS / FAIL**.

| ID | Scenario | Expected Brain behaviour |
|---|---|---|
| B1 | Calm day + rush hour | No incident, no command |
| B2 | Real flood at the port | Incident → valve opened → ack → water falls → incident closed |
| B3 | Fake flood (Guardian rejects the sensor) | No action; reason on the timeline |
| B4 | Guardian offline during a flood | Safe mode; low-risk actions only, valve waits |
| B5 | Fire + accident, one ambulance | Higher-priority incident gets the ambulance |
| B6 | Stuck valve | No effect → command failed → alternative chosen |
| B7 | Ten fake fire calls in one minute | Brain doesn't send all units |
| B8 | Best valve quarantined | Another valve used, refusal shown |
| B9 | Bad messages (HMAC, stale, replay, unknown id, range) | Each rejected with the right error code |
| B10 | Prompt injection in an emergency call | No effect |
| B11 | Fire + power spike + jam next door | Recognised as one incident (LLM) |
| B12 | LLM offline during a flood | Rule fallback acts |

## 7. Tasks

### Week 1 (15–19 Oct): priority, others are waiting
- [ ] Mock city file; mock Twin publishing `twin/meta` and readings with envelope + HMAC (check against the spec's test vector)
- [ ] Mock Guardian `trust_all`; mock actuators `ok`
- [ ] Share the test keys and broker config with all teams

### Week 2 (20–26 Oct)
- [ ] Calls, unit status, actuator effects on readings, all Guardian and actuator modes, fault injection
- [ ] Scenario format + runner with PASS/FAIL; scenarios B1, B2, B3
- [ ] **Sun 26 Oct:** end-to-end run on mocks

### Week 3 (27 Oct – 2 Nov)
- [ ] Scenarios B4–B12
- [ ] Optional: **replay real data** recorded from the Twin team's real sensors in the mock

### Week 4 (3–7 Nov)
- [ ] Test Book entries for all Brain scenarios (ID, name, layers, description, trigger, expected result per layer, how to verify)
- [ ] Report: mocks and scenarios

## 8. Done when

- [ ] `docker compose up` starts broker + mocks; the Brain runs alone against them
- [ ] All 12 scenarios run with one command each, produce one log file, print PASS/FAIL
- [ ] The mocks pass the interface checks of the input team without any special case
- [ ] Switching to the real Twin / Guardian only changes the broker config

## 9. Who you work with

| Team | About |
|---|---|
| **Input** | Envelope, HMAC, test keys, fault injection |
| **Graph** | Realistic noise, rush hour, scenario data |
| **Math, Output** | Competing incidents, limited units, quarantine, stuck actuators |
| **LLM** | Calls with free text and injections |
| **Team lead** | Test Book format shared with the Twin and Guardian teams |

## 10. Risks and tips

- Start ugly but early. A mock that publishes one sensor on day 2 helps more than a perfect mock on day 10.
- Use a fixed random seed per scenario so results are repeatable.
- Keep scenario timings short (≤ 2 minutes each): the live demo has 8 minutes.
