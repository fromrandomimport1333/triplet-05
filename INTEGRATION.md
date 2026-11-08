# Integration

How the three layers connect: diagram, protocol, and one scenario traced end to end.

## Diagram

```
                       ┌──────────────────── MQTT broker (Mosquitto, TLS :8883) ────────────────────┐
                       │      one account per layer · topic ACL · retained state · Last Will        │
                       └───────▲──────────────────────────▲───────────────────────────▲─────────────┘
     twin/meta, city/…/sensor/…, city/…/call,             │ guardian/verdict/…,          │ brain/cmd/…, brain/event,
     twin/actuator/…, twin/unit/…, twin/status            │ guardian/quarantine/…,       │ brain/error, brain/alert,
                       │                                  │ guardian/status              │ brain/status
                ┌──────┴──────┐                    ┌──────┴──────┐                ┌───────┴────────┐
                │    TWIN     │ readings ────────▶ │  GUARDIAN   │ verdicts ────▶ │   CITY BRAIN   │
                │ city, units │ ◀──── commands ─── │ trust, quar.│ ◀──── alerts ─ │ detect, decide │
                └─────────────┘                    └─────────────┘                │ act, explain   │
                       ▲                                                          └───────┬────────┘
                       └──────────────────── signed commands, acknowledged ───────────────┘
                                                                                 live timeline :8090
```

The Brain runs alone with stand-ins of the Twin and the Guardian (`brain/mocks/`): they speak exactly
this protocol, so replacing them by the real layers changes only the broker configuration (R1).

## Protocol

| | |
|---|---|
| Transport | MQTT 3.1.1 over TLS, triplet CA, broker `brain/broker/` (`docker compose up broker`) |
| Accounts | `twin`, `guardian`, `brain`, `tester` (scenario runner); the ACL lets each write only its own topics |
| Envelope | `{"v": 1, "body": "<JSON text>", "hmac": "<hex>"}` or `"hmacs": {layer: hex}`; HMAC-SHA256 over the body text, one key per pair of layers |
| Body | `id`, `timestamp` (ISO UTC, ms), `source`, `correlation_id` (the message that started the chain), `seq` for readings |
| Freshness | ±10 s; ids unique for 60 s; `seq` increasing per device |
| Liveness | `<layer>/status` `online`, Last Will `offline`, retained |
| Formats | every topic and an example body: `brain/INTERFACE.md`; JSON Schemas: `brain/contracts/schemas/` |

## Shared decisions

| Decision | Why |
|---|---|
| One MQTT broker over TLS, one account per layer, topic ACL | Pub/sub fits three layers plus a timeline; a layer can only speak for itself |
| HMAC-SHA256 envelope, one key per pair of layers | End-to-end integrity through the broker; no layer can forge another's messages |
| `correlation_id` copied along every chain | The timeline and the graph can show cause and effect (R3) |
| Retained city description, verdicts, quarantines and statuses | A restarted layer gets the whole current state at once |
| Status with Last Will for every layer | Partner failure is detected in seconds, and each layer degrades safely (Brain: safe mode without Guardian, no commands without Twin) |
| Error codes on refused input (`E_AUTH`, `E_STALE`, `E_REPLAY`, `E_FORMAT`, `E_UNKNOWN_ID`, `E_RANGE`) | A partner sees why a message was refused |
| Everything partner-specific in configuration | Phase 2 reshuffles triplets: connecting to new partners must not need code changes |

## One scenario end to end: B2, real flood at the port

`python scenarios/run.py B2` — trace from `logs/B2-*.log` (times from the start of the run):

| t (s) | Layer | What happens |
|---|---|---|
| 0.00 | runner | `twin/control` reset: flood at zone-01 (Port) after 5 calm ticks |
| 6.58 | GUARDIAN | `guardian/verdict/water-01` **trusted** (0.95): a real, noisy rise |
| 7.92 | TWIN | `city/zone-01/call` flood: *"Water is coming over the quay in zone-01, the street is flooded"* |
| 7.96 | BRAIN | incident opened: flood at zone-01 (confidence 0.88, high): water-01 at alert level + the call |
| 8.01 | BRAIN | rule proposal: open valve-01, priority signal-01, dispatch unit-rescue-01 |
| 8.03 | BRAIN | LLM proposal: open valve-01, priority signal-01 |
| 8.03 | BRAIN | decision: *rules proposal (score 6.73) chosen over llm (4.64)*; safety rules S1–S7 pass |
| 8.03 | BRAIN | `brain/cmd/valve-01` open `{"opening": 1.0}`, `brain/cmd/signal-01` priority, `brain/cmd/unit-rescue-01` dispatch `{"zone_id": "zone-01"}` (signed) |
| 9.91 | TWIN | `twin/actuator/valve-01` **done**, state `open_100`; signal and rescue unit acknowledged |
| 10.00 | BRAIN | effect confirmed: traffic-speed-01 rose from 45 to 53 km/h after the priority |
| 45.00 | runner | `twin/control` trigger calm: the water falls |
| 48.16 | BRAIN | incident closed: no live evidence left |

From the alert-level reading confirmed by a resident's call to signed commands: **0.07 s**; from
commands to the Twin's acknowledgement: 1.9 s (the Twin applies commands two ticks later).

The same chain is shown live on the timeline (rows of one chain share a colour bar) and returned by
the graph API: `GET http://127.0.0.1:8080/chain/<correlation_id>`.

## Running the triplet

```bash
cd brain && python tools/setup.py          # once: certificates, keys, accounts (give ca.crt + keys to the other teams)
docker compose up                          # broker + Brain + stand-ins
python scenarios/run.py --all              # every Test Book scenario, one log each in logs/
```

To connect the real Twin and Guardian: they use the broker address, their account and their pair
key; the Brain is started without its stand-ins (`python run.py`, or `BRAIN_CMD="python run.py"`
with the `real-layers` profile). Nothing in the Brain's code changes.
