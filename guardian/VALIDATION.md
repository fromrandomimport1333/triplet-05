# Validation report — Guardian v1.3 (Brain protocol profile)

Run on 2026-11-07, Python 3.12, no broker, no Brain repository, no paho-mqtt installed.

## Results

```
$ cd guardian && python -m pytest -q tests
132 passed, 1 skipped            (62 native tests unchanged + 70 in tests/brain;
                                  skipped = test_pg_store.py, needs GUARDIAN_TEST_DATABASE_URL)

$ bash scenarios/run_guardian_scenarios.sh local
PASS G1_stuck_sensor   PASS G2_spoofed_flood   PASS G3_replay_attack   PASS G4_brain_ignores_fake
PASS G5_quarantine_midflow   PASS G6_brain_fake_flood_t5   PASS G7_inconsistent_ack
PASS G8_meta_units   PASS G9_bad_hmac_verdict   PASS G10_frozen_sensor
PASS G11_verdict_refresh                                 (one log each in logs/)
```

G6 (T5): first fake at t = 10 s → `rejected / implausible_jump` at t = 10 s; quarantine notice at t = 12 s.
G10: freeze from t = 10 s → `suspicious / frozen` at t = 17 s (8th identical reading).
G11: Twin silent 120 s → 6 refreshes, largest gap without a verdict 20 s.

## Verified (run here) vs not run

| Item | Status |
|---|---|
| Native protocol + all 62 pre-existing tests | ✅ run, pass |
| Kit HMAC test vector, key whitespace stripping, ms timestamps | ✅ unit tests |
| Every Guardian→Brain body valid against external-verdict / external-quarantine / external-status (jsonschema) | ✅ contract tests |
| Kit example bodies (meta, reading, actuator, unit, brain command, brain alert, brain error) validate and parse | ✅ contract tests |
| A3-A6, B1 (acks), B2 (control reset), C behaviours B1/B2/B3=T5/B7/B9 on the **synthetic** Twin | ✅ unit tests + G6-G9 |
| Every topic we publish inside `guardian/#` | ✅ test |
| v1.3: verdict refresh (< 30 s gaps while the Twin is silent) and frozen rule (8 identical readings), refreshed and frozen bodies valid against external-verdict | ✅ unit tests + G10, G11 |
| Last Will set before connect, status online after connect, subscriptions, retain flag | ✅ with a fake paho client only |
| Real MQTT/TLS connection, account/password, broker ACL | ❌ not run (no broker, paho not installed here) |
| `partner_check.py --layer guardian` | ❌ not run (no Brain broker, no ca.crt / password / key) |
| Kit scenarios B1, B2, B3/T5 against the Brain repo and its mock Twin | ❌ not run (Brain repository not available) |
| Reading period / noise of the Brain's mock Twin | ❌ not measured; calibration derived from kit figures (INTERFACE.md §8) |
| Native live mode (docker stack) | ❌ not run (no Docker here) |
| PgStore against TimescaleDB | ❌ not run here (CI job exists) |

## Open questions for the Twin and Brain teams

1. **Brain — ACL:** add for the twin account `topic read guardian/quarantine/#` and `topic read guardian/control/#`?
2. **Brain — guardian/control:** exact formats of `mode`, `offline`, `online` (brain/mocks/guardian/) and which modes B5/B8 use to quarantine unit-ambulance-02 / valve-01 at start. Which key seals it: guardian-brain.key (our assumption)? Retained or not?
3. **Brain — brain/error:** is it sealed, and with which key?
4. **Brain — liveness:** while the Twin is silent we re-send the current verdict of each sensor every 20 s (new id, same correlation_id, details "refresh …"). Does the Brain accept that as a live verdict, or must a verdict always follow a new reading?
5. **Brain — verdicts for refused inputs:** do you want a verdict for duplicates (`replay`, not retained) or is audit-only fine (our default)?
6. **Twin — readings:** always `hmacs.guardian`? Integer `seq` on every reading? After a restart, do you publish a new twin/meta id (we reset the seq table on a new meta id)?
7. **Twin — twin/meta:** will it carry `hmacs.guardian`? (Without it we accept and log it.)
8. **Twin — acks:** do state updates ever carry `result` without a command (we would count them as acks with no known command)?
9. **Twin — units:** confirm the exact unit strings and the reading period (we assume 1 s) and noise per type, to replace our derived thresholds by measured ones.

10. **Twin — freeze fault:** does `freeze` repeat exactly the same value (we flag 8 identical readings), or the same value with noise?
