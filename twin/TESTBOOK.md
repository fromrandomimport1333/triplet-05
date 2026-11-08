# Test Book

Generated from `scenarios/scenarios.py` by `python scenarios\run_scenario.py --testbook`. Do not edit by hand.
Each scenario starts with one command and writes `logs/<id>-<date>-<time>.log` (timeline and PASS/FAIL) and a `.json` summary.
Add `--layers real` to run it against the partner teams' Brain and Guardian instead of the Twin team's stand-ins.

| ID | Scenario | Phase 0 report (Table 10) | Layers |
|---|---|---|---|
| T1 | Calm day, no false alarms | Phase 0 test plan | Twin, Guardian, Brain |
| T2 | Real flood: valve opens, alert sent | Phase 0 test plan | Twin, Guardian, Brain |
| T3 | Spoofed flood: attack is blocked | Phase 0 test plan | Twin, Guardian, Brain |
| T4 | Stuck air sensor detected | Phase 0 test plan | Twin, Guardian |
| T5 | Replayed readings rejected | Phase 0 test plan | Twin, Guardian |
| T6 | Real jam + simultaneous attack, then Guardian killed | Phase 0 test plan | Twin, Guardian, Brain |
| T7 | Stuck drainage valve: Brain closes the road and dispatches | Phase 0 test plan | Twin, Guardian, Brain |
| T8 | Tampered message rejected | added (integrity check) | Twin, Guardian |
| T9 | Drifting sensor detected | added (fault kind drift) | Twin, Guardian |
| T10 | Network flooding detected | added (fault kind flood_network) | Twin, Guardian |
| T11 | Dropout detected | added (fault kind dropout) | Twin, Guardian |
| T12 | Impossible value rejected | added (fault kind spoof) | Twin, Guardian |

## T1: Calm day, no false alarms

| Field | Value |
|---|---|
| ID | T1 |
| Name | Calm day, no false alarms |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian, Brain |
| Description | Normal running with no rain and no fault for 30 seconds. Shows the layers stay quiet when nothing is wrong. |
| Trigger | `python scenarios\run_scenario.py t1` |
| Expected: Twin | Publishes signed readings about once per second; valve stays closed. |
| Expected: Guardian | Every sensor trusted; no untrusted verdict. |
| Expected: Brain | No decision taken. |
| How to verify | Timeline screen; log file `logs/t1-*.log`; the PASS/FAIL lines at its end |

## T2: Real flood: valve opens, alert sent

| Field | Value |
|---|---|
| ID | T2 |
| Name | Real flood: valve opens, alert sent |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian, Brain |
| Description | Heavy rain (60 mm/h) raises the canal above the Brain's flood threshold. The Brain must act, because the data is real. |
| Trigger | `python scenarios\run_scenario.py t2` |
| Expected: Twin | Water level rises; VALVE_03 confirms 'open'; public alert shown; valve closes after the flood. |
| Expected: Guardian | Water sensors stay trusted (a real flood is not an attack). |
| Expected: Brain | flood_detected, verification (trusted), command_sent with verdict_ref (valve open, public alert), actuator_result ok, later flood_cleared and valve closed. |
| How to verify | Timeline screen; log file `logs/t2-*.log`; the PASS/FAIL lines at its end |

## T3: Spoofed flood: attack is blocked

| Field | Value |
|---|---|
| ID | T3 |
| Name | Spoofed flood: attack is blocked |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian, Brain |
| Description | An attacker makes WATER_08 report 4.2 m while the true level is about 0.4 m. The Brain must NOT open the valve. |
| Trigger | `python scenarios\run_scenario.py t3` |
| Expected: Twin | Publishes 4.2 m labelled fault=spoof; ground truth stays low; valve untouched; sensor quarantined. |
| Expected: Guardian | WATER_08 untrusted ('jumped from ... to 4.2'); quarantined after 3 bad windows, released after 3 clean ones. |
| Expected: Brain | flood_detected then action_blocked with the Guardian's reason; no valve command. |
| How to verify | Timeline screen; log file `logs/t3-*.log`; the PASS/FAIL lines at its end |

## T4: Stuck air sensor detected

| Field | Value |
|---|---|
| ID | T4 |
| Name | Stuck air sensor detected |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian |
| Description | AIR_01 keeps repeating the same value. |
| Trigger | `python scenarios\run_scenario.py t4` |
| Expected: Twin | Same value repeated, labelled fault=stuck. |
| Expected: Guardian | AIR_01 untrusted: 'value unchanged for 10+ readings'. |
| Expected: Brain | No decision (air quality is not used by the stand-in Brain). |
| How to verify | Timeline screen; log file `logs/t4-*.log`; the PASS/FAIL lines at its end |

## T5: Replayed readings rejected

| Field | Value |
|---|---|
| ID | T5 |
| Name | Replayed readings rejected |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian |
| Description | (a) A genuine AIR_04 reading is re-sent unchanged 12 s later; (b) the Twin's replay fault re-publishes a stream of old signed AIR_04 readings with their old seq and ts. |
| Trigger | `python scenarios\run_scenario.py t5` |
| Expected: Twin | (b) old signed messages instead of fresh ones, ground truth unchanged. |
| Expected: Guardian | AIR_04 untrusted: 'replayed or repeated message' or 'timestamp is N s old'. |
| Expected: Brain | No action on the replayed data. |
| How to verify | Timeline screen; log file `logs/t5-*.log`; the PASS/FAIL lines at its end |

## T6: Real jam + simultaneous attack, then Guardian killed

| Field | Value |
|---|---|
| ID | T6 |
| Name | Real jam + simultaneous attack, then Guardian killed |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian, Brain |
| Description | A real jam builds up in zone_a while WATER_08 is spoofed to 4.2 m. The Brain must handle the jam and block the fake flood. Then the Guardian process is killed and WATER_07 is spoofed too: the Brain must fail closed (degraded mode). Finally the Guardian is restarted. |
| Trigger | `python scenarios\run_scenario.py t6` |
| Expected: Twin | Cameras in zone_a slow down; TL_01 confirms its new phase; keeps publishing while the Guardian is down. |
| Expected: Guardian | Cameras trusted, WATER_08 untrusted; then down; then back. |
| Expected: Brain | jam_detected, signal command with verdict_ref; action_blocked for WATER_08; after the kill: action_blocked verdict=unreachable (tier B valve) and operator_request (tier A alert); no valve command. |
| How to verify | Timeline screen; log file `logs/t6-*.log`; the PASS/FAIL lines at its end |

## T7: Stuck drainage valve: Brain closes the road and dispatches

| Field | Value |
|---|---|
| ID | T7 |
| Name | Stuck drainage valve: Brain closes the road and dispatches |
| Phase 0 report | Phase 0 test plan |
| Layers involved | Twin, Guardian, Brain |
| Description | VALVE_03 is stuck (fault actuator_stuck) when a real flood starts. The valve command fails, so the Brain closes ROAD_01 and dispatches civil protection instead. |
| Trigger | `python scenarios\run_scenario.py t7` |
| Expected: Twin | VALVE_03 feedback ok=false; ROAD_01 closed; DISPATCH_01 'dispatched' with unit and ETA, then 'arrived'. |
| Expected: Guardian | Water sensors stay trusted. |
| Expected: Brain | flood_detected, valve command, action_failed, fallback, road closed, civil_protection dispatched, alert; after the flood: flood_cleared and road reopened. |
| How to verify | Timeline screen; log file `logs/t7-*.log`; the PASS/FAIL lines at its end |

## T8: Tampered message rejected

| Field | Value |
|---|---|
| ID | T8 |
| Name | Tampered message rejected |
| Phase 0 report | added (integrity check) |
| Layers involved | Twin, Guardian |
| Description | A reading is copied, its value is changed and it is published again with the old signature. |
| Trigger | `python scenarios\run_scenario.py t8` |
| Expected: Twin | Unchanged; it is not the source of this message. |
| Expected: Guardian | AIR_04 untrusted: 'signature invalid'; not quarantined. |
| Expected: Brain | No decision. |
| How to verify | Timeline screen; log file `logs/t8-*.log`; the PASS/FAIL lines at its end |

## T9: Drifting sensor detected

| Field | Value |
|---|---|
| ID | T9 |
| Name | Drifting sensor detected |
| Phase 0 report | added (fault kind drift) |
| Layers involved | Twin, Guardian |
| Description | AIR_03 drifts upwards by 4 AQI per second (fault drift). |
| Trigger | `python scenarios\run_scenario.py t9` |
| Expected: Twin | Value drifts, labelled fault=drift; true value unchanged. |
| Expected: Guardian | AIR_03 untrusted once it disagrees with its zone peer AIR_04. |
| Expected: Brain | No decision. |
| How to verify | Timeline screen; log file `logs/t9-*.log`; the PASS/FAIL lines at its end |

## T10: Network flooding detected

| Field | Value |
|---|---|
| ID | T10 |
| Name | Network flooding detected |
| Phase 0 report | added (fault kind flood_network) |
| Layers involved | Twin, Guardian |
| Description | POWER_02 publishes 20 extra messages per reading (fault flood_network). |
| Trigger | `python scenarios\run_scenario.py t10` |
| Expected: Twin | Burst of signed POWER_02 messages. |
| Expected: Guardian | POWER_02 untrusted: 'message rate ... (network flooding)'; others unaffected. |
| Expected: Brain | No decision. |
| How to verify | Timeline screen; log file `logs/t10-*.log`; the PASS/FAIL lines at its end |

## T11: Dropout detected

| Field | Value |
|---|---|
| ID | T11 |
| Name | Dropout detected |
| Phase 0 report | added (fault kind dropout) |
| Layers involved | Twin, Guardian |
| Description | POWER_01 stops sending data. |
| Trigger | `python scenarios\run_scenario.py t11` |
| Expected: Twin | No POWER_01 readings while the fault lasts. |
| Expected: Guardian | POWER_01 untrusted: 'no data for N s'. |
| Expected: Brain | No decision. |
| How to verify | Timeline screen; log file `logs/t11-*.log`; the PASS/FAIL lines at its end |

## T12: Impossible value rejected

| Field | Value |
|---|---|
| ID | T12 |
| Name | Impossible value rejected |
| Phase 0 report | added (fault kind spoof) |
| Layers involved | Twin, Guardian |
| Description | AIR_03 reports an air quality index of 999, which is above the physical range. |
| Trigger | `python scenarios\run_scenario.py t12` |
| Expected: Twin | Publishes 999 labelled fault=spoof. |
| Expected: Guardian | AIR_03 untrusted: 'outside the possible range'. |
| Expected: Brain | No decision. |
| How to verify | Timeline screen; log file `logs/t12-*.log`; the PASS/FAIL lines at its end |
