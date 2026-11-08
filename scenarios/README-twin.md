# Scenarios (rule R2) and live timeline (rule R3)

Each scenario starts with **one command** and leaves a log file in the root `logs/` folder.

Start the city first with one command: `.\start.ps1` (Windows, no Docker) or `docker compose -f docker-compose.twin.yml up --build`.
This starts Mosquitto, the Twin in `--fake` mode, the Guardian and Brain stand-ins and the timeline (http://127.0.0.1:8002).

```
python scenarios\run_scenario.py --list              # the 12 scenarios
python scenarios\run_scenario.py t3                  # spoofed flood, against our stand-ins
python scenarios\run_scenario.py t3 --layers real    # the same, against the partner teams' Brain and Guardian
python scenarios\run_scenario.py t1,t3,t6            # several, one after the other
python scenarios\run_scenario.py all                 # everything (about 25 minutes)
python scenarios\run_scenario.py --testbook          # regenerate twin/TESTBOOK.md from scenarios.py
python scripts\kpis.py                               # Phase 0 KPIs from the latest log of each scenario
```

Every run first calms the city (Twin reset, quarantines released, Guardian restarted if it is down, every sensor trusted), then triggers the
scenario, then checks each layer's reaction and writes

- `logs/<id>-<date>-<time>.log`: a timestamped timeline (SCENARIO / TWIN / GUARDIAN / BRAIN / ACTUATOR lines) followed by PASS, FAIL or SKIP for each expected result;
- `logs/<id>-<date>-<time>.json`: the same run in numbers, used by `scripts/kpis.py`.

The exit code is 0 only if all checks passed.

`config.yaml` holds the two profiles: `mocks` (our stand-ins, the default) and `real` (the partner layers). Environment variables override both
(`TWIN_URL`, `GUARDIAN_URL`, `BRAIN_URL`, `GUARDIAN_API_KEY`, `TWIN_TEST_KEY`, `MQTT_HOST`, `MQTT_PORT`, `TSYP_HMAC_KEY`). The checks use only the
agreed interface (MQTT topics, `GET /verdict/<id>` and `/health` on the Guardian, `brain/decisions`, the Twin's API); checks that only make sense for
our stand-ins are marked SKIP with `--layers real`. Scenario T6 stops the Guardian by its port and restarts it with `guardian_start`; for the real
Guardian set `guardian_kill` / `guardian_start` in `config.yaml` (for example to `docker compose stop guardian` / `start guardian`).

Scenarios live in `scenarios.py`: add a function and an entry to `SCENARIOS`, then regenerate the Test Book.
