# City Brain: mocks and scenarios (report paragraph)

**What.** Three stand-ins let the City Brain run and be tested alone (R1): a Twin mock (a six-zone
city with 44 sensors (a smoke, CO, CO₂ and temperature signature in two zones), 12 actuators and 6 response units, scripted incidents, rush hour, emergency
calls with free text, fault injection, actuator modes), a Guardian mock (trust verdicts,
quarantine, status) and a mock LLM. On top of them, 14 Test Book scenarios (B1-B14, plus T5, the
organizers' fake-flood example) each start with one command, `python scenarios/run.py <ID>`, and
leave one log file in `logs/` (R2). `TESTBOOK.md` is generated from the scenario files.

**How.** Every mock speaks the real protocol: same MQTT topics (read from `config/topics.yaml`),
same sealed envelopes and HMAC keys, same message fields, so the Brain's input module accepts them
with no special case; the tests prove it by feeding each mock's output to the real input module.
Each mock is a pure core (no network, no clock) behind a small service and a `__main__` that
connects it to the broker, so its logic is unit-tested without a broker. The Guardian mock runs
real, lightweight checks on every reading: range, implausible jump per sensor type, frozen value,
flatline at a bound, and a per-sensor baseline. A jump stays rejected until the sensor settles at a
new level with live noise, so a forged constant never becomes trusted. A changed verdict is
published at once (the T5 fake reading is rejected and the sensor isolated at its first reading,
well inside 10 s) and unchanged verdicts are refreshed every 5 s so the Brain never mistakes a calm
city for a silent Guardian. Forced modes (trust all, reject or suspect one sensor, flip-flop,
quarantine any device, slow, offline) reproduce the partner failures of team plan 06. The runner
publishes `brain/control/run` start / stop for the timeline, drives the mocks through their control
topics at scripted offsets, records every message it sees and checks a declarative `expect` list
(event seen within n seconds, no command to a valve, at most one fire truck, error code E_AUTH,
decision origin rules, ...), printing PASS / FAIL per check.

**Why.** Repeatability and evidence: the same seed and control messages give the same city, so a
failure can be replayed; one log per run lets the jury check every scenario after the event; the
checks are pure functions over a list of messages, tested on synthetic runs, and each scenario's
plan is replayed through the mocks and the input module in the test suite, so a broken scenario
file or mock is caught before a demo. Scenario files carry the official Test Book fields, so the
Test Book cannot drift from what is actually run.

**Limits.** Scenario effects are scripted steps, not city physics (a flood rises 0.15 m/s from
1.4 m, a valve takes 0.3 m off); the Guardian's jump limits are tuned to the mock's steps, so a
different Twin may need other values in `config/guardian-mock.yaml`. The Guardian mock does not
isolate a sensor inside the Twin (its quarantine notice goes to the Brain only), and judges each
sensor alone (no cross-sensor correlation). Expectations on incidents, decisions and commands
describe the behaviour agreed in the team plan; they can only pass end to end, with every Brain
module running, and some (for example the two-sensor rule S3 against a city with one water sensor
per zone) must be checked against the final safety settings. Timings use the wall clock and a 1 s
tick, so `within_s` windows include a few seconds of margin.
