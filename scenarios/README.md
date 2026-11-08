# Scenarios

One YAML file per Test Book scenario, one command per run, one log file per run (Phase 1 rule R2).
`TESTBOOK.md` at the repo root is generated from these files.

```
python scenarios/run.py B3          # run one scenario (also ./scenarios/run B3, scenarios\run.cmd B3, .\scenarios\run.ps1 B3)
python scenarios/run.py --all       # all of them, in turn
python scenarios/run.py --list      # ids, durations, names
python scenarios/run.py --check     # validate the files (no broker)
python scenarios/run.py B3 --plan   # the control messages a run sends (no broker)
python scenarios/make_testbook.py   # regenerate TESTBOOK.md after editing a file
```

| ID | Scenario | ID | Scenario |
|---|---|---|---|
| B1 | Calm day with a rush hour | B8 | Best valve quarantined |
| B2 | Real flood at the port | B9 | Bad messages from the Twin |
| B3 | Fake flood at the port (= organizer example T5) | B10 | Prompt injection in an emergency call |
| B4 | Guardian offline during a flood | B11 | Fire, power spike and a jam next door |
| B5 | Fire and accident, one ambulance | B12 | LLM offline during a flood |
| B6 | Stuck valve | B13 | Twin goes offline |
| B7 | Ten fake fire calls in one minute | B14 | Burnt toast: smoke without fire (case memory) |
| | | T5 | Same run as B3, under the organizers' id |

## Before a run

Broker, the Brain modules and the three mocks running, from `brain/` (keys and broker of
`brain/README.md`): `python -m mocks.twin`, `python -m mocks.guardian`, `python -m mocks.mock_llm`.
The runner uses the Brain's broker settings and keys (`brain/config/broker.yaml`, `security.yaml`;
`BRAIN_CONFIG_DIR` is honoured) and the Python of `brain/.venv` when it exists.

## What a run does

1. Subscribes to what it checks: `brain/event`, `brain/error`, `brain/alert`, `brain/cmd/+`,
   `brain/internal/{incident,decision,outcome,proposal,layer-status/+}`, Guardian verdicts,
   quarantines and status, Twin acknowledgements, unit states, calls and status, plus the readings a
   scenario names. Retained messages are kept in the log but never count for a check.
2. Publishes `brain/control/run` `{run_id, scenario_id, action: start, ts}` (retained): the timeline
   starts a new log.
3. Sends the control messages of the scenario at their time offsets: mock LLM mode on
   `brain/control/llm`, Guardian `reset` (+ modes) on `guardian/control`, Twin `reset` (seed,
   scenario, zone, start tick, rush hour, scripted calls) and `faults` on `twin/control`, then the
   timed actions (`trigger`, `call`, `contain`, `offline`, `online`, Guardian modes, LLM modes).
   Twin and Guardian control messages are sealed with their HMAC keys.
4. Waits `duration_s` (+ 2 s), evaluates the `expect` list, prints PASS / FAIL per check and overall,
   publishes `brain/control/run` stop (with the result), puts the Guardian and the mock LLM back to
   their defaults (`cleanup: false` in a file to skip that).
5. Writes `logs/<ID>-<YYYYMMDD-HHMMSS>.log`: JSON lines `run`, then every `sent` and `observed`
   message in time order (`t` = seconds since the start, `ts`, `topic`, decoded `body`), then one
   `summary` line with the result of each check.

Exit code 0 = every check passed, 1 = a check failed, 2 = could not run (no broker, bad file).

## File format

Official Test Book fields as YAML keys: `id`, `name`, `layers` (twin / guardian / brain),
`description`, `trigger` (`python scenarios/run.py <ID>`), `expected` (`twin`, `guardian`, `brain`),
`how_to_verify`. Machine fields: `duration_s` (at most 90), `observe.readings`, `twin` (`reset`,
`faults` with Twin ticks counted from the reset, `actuator_modes`, `actions` with `at_s` and an
optional `repeat: {count, every_s}`), `guardian` (`modes`, `actions`), `llm` (`mode`, `actions`),
`expect`. `alias_of: B3` reuses another file and overrides its Test Book fields (T5).

Checks (`expect`), one kind per entry:

| Kind | Reads | Short form |
|---|---|---|
| `event` | `brain/event` (and `incident_opened/updated/closed` from `brain/internal/incident`) | event name |
| `error_code` | `brain/error` | code |
| `alert` | `brain/alert` | alert name |
| `command` | `brain/cmd/+` | action |
| `incident`, `decision`, `outcome`, `proposal` | `brain/internal/...` | type, origin, status, origin |
| `layer` | `brain/internal/layer-status/+` | layer |
| `verdict`, `quarantine` | `guardian/verdict/+`, `guardian/quarantine/+` | verdict, state |
| `ack` | `twin/actuator/+`, `twin/unit/+` | result |
| `reading` | the readings in `observe.readings` (added automatically) | sensor id |
| `no_<kind>` | the same, must not appear | |
| `max_commands` | commands matching the filters, a retry with the same id counted once | `{count: 1, ...}` |
| `any_of` | passes if one of its checks passes | list of checks |

Modifiers: `match` (body fields; `zone` = `zone_id`; a list = one of; `{lt, le, gt, ge}` for
numbers), `text` (substring of the body), `after_s` / `within_s` (window, seconds after the start),
`count` (at least n), `optional: true` (reported as WARN, never fails the run). Filters
`target_type`, `target_id`, `action`, `target_zone` look at the command, at each action of a
decision or proposal, or at the target of an outcome, alert or acknowledgement; types come from
`brain/internal/meta` (else `brain/mocks/city.yaml`). `any` matches everything (`no_command: any`).

## Tests

`cd brain && python -m pytest tests/scenarios`: every file is valid and matches the mock city,
`TESTBOOK.md` is up to date, the evaluation works on synthetic message lists, the runner (fake
broker and clock) publishes start / stop and writes one log, and each scenario's control plan,
played through the Twin mock, the Guardian mock and the Brain's real input module, satisfies every
check those three can satisfy alone (readings, verdicts, quarantines, input errors, layer status).
The checks on incidents, decisions and commands need the rest of the Brain and run end to end only.
