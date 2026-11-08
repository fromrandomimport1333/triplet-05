# Team 1: Input

**People:** 1 · **Code:** `brain/src/input/` · **Read first:** [`00-overview-and-contracts.md`](00-overview-and-contracts.md), [`../city-brain-interface-spec.pdf`](../city-brain-interface-spec.pdf) sections 3–5 and 9

## 1. Mission

You are the **front door of the City Brain**. Every message from the Twin and Guardian passes through you. Nothing reaches the rest of the Brain unless it is **genuine, fresh, well-formed and makes sense**. You turn external messages into the clean internal messages every other team relies on.

## 2. You own / you don't own

| You own | You don't own |
|---|---|
| MQTT connection of the Brain to the shared broker (TLS / mTLS) | Deciding if a sensor tells the truth (Guardian's job) |
| Subscribing to all Twin and Guardian topics | Detecting incidents (Graph team) |
| ① Checks: HMAC, timestamp, id (replay), format, known IDs, ranges | Signing outgoing messages (Output team) |
| Publishing clean internal messages (`brain/internal/…`) | |
| Publishing rejected messages on `brain/error` with error codes | |
| Tracking Twin / Guardian online / offline (Last Will) | |
| Brain's own `brain/status` with Last Will | |

## 3. Your inputs and outputs

**Inputs (external, from the interface spec):**

| Topic | From | Content |
|---|---|---|
| `twin/meta` | Twin | City description (retained) |
| `city/{zone}/sensor/{type}/{sensor_id}` | Twin | Sensor readings |
| `city/{zone}/call` | Twin | Emergency calls |
| `twin/actuator/{id}`, `twin/unit/{id}` | Twin | Acknowledgements, actuator and unit state |
| `guardian/verdict/{sensor_id}` | Guardian | Trust verdicts |
| `guardian/quarantine/{device_id}` | Guardian | Quarantine notices |
| `twin/status`, `guardian/status` | Twin, Guardian | Online / offline |

⚠️ Topic names may change after alignment with the Guardian team (they use `twin/events/#`). **Keep every topic name in `config/topics.yaml`.**

**Outputs (internal):** `brain/internal/meta`, `reading`, `call`, `trust/{sensor_id}`, `quarantine/{device_id}`, `ack`, `layer-status/{layer}`, plus `brain/error` and `brain/status`. Formats in the overview, section 3.

## 4. What your checks must do

Run in this order; stop at the first failure.

| # | Check | Rule | Error code |
|---|---|---|---|
| 1 | Envelope | Valid JSON with `v`, `body`, `hmac` (or `hmacs`) | `E_FORMAT` |
| 2 | Signature | HMAC-SHA256 of the `body` text with the Twin–Brain key equals `hmac` (or `hmacs.brain`). For Guardian: HMAC now, **Ed25519** if adopted (see alignment doc) | `E_AUTH` |
| 3 | Freshness | `timestamp` within ±10 s of our clock | `E_STALE` |
| 4 | Replay | `id` not seen in the last 60 s (and `seq` increasing per device, if the Twin adds it) | `E_REPLAY` |
| 5 | Format | Required fields present with the right types (pydantic) | `E_FORMAT` |
| 6 | Known IDs | Sensor, zone, actuator, unit declared in `twin/meta` | `E_UNKNOWN_ID` |
| 7 | Range | Value between the sensor's `min` and `max` | `E_RANGE` |

**Important:** a value outside `min`/`max` is rejected by you, but a value that is *possible yet suspicious* (e.g. water rising too fast) is **not** your job. That is Guardian's and detection's.

Every rejection: log it, publish on `brain/error`:
```json
{"id": "in-err-00031", "timestamp": "2026-11-14T10:22:03.160Z", "source": "brain",
 "code": "E_RANGE", "topic": "city/port/sensor/water_level/water-level-04",
 "message_id": "twin-000199", "detail": "value -5.0 outside [0.0, 4.0]"}
```

## 5. Tasks

### Week 1 (15–19 Oct)
- [ ] MQTT client in `src/common/mqtt.py` (shared with all teams): connect with TLS, auto-reconnect, re-subscribe, Last Will `brain/status = offline`
- [ ] `config/topics.yaml` and `config/security.yaml` (paths to CA, client certificate, keys)
- [ ] pydantic models for all **external** messages in `src/common/models.py`
- [ ] Envelope + HMAC verification, checked against the **test vector** of the spec (annex B)
- [ ] **Fri 17 Oct:** internal formats frozen with the other teams

### Week 2 (20–26 Oct)
- [ ] Freshness, replay cache, format, known IDs, ranges
- [ ] Publish all internal topics; keep `meta`, `trust`, `quarantine`, `layer-status` **retained**
- [ ] `brain/error` with codes
- [ ] Layer status from `twin/status` / `guardian/status`; also mark Guardian offline if no verdict arrives for 30 s
- [ ] **Sun 26 Oct:** end-to-end run on mocks

### Week 3 (27 Oct – 2 Nov)
- [ ] Switch to **mTLS** and **Ed25519** for Guardian if agreed
- [ ] Joint run with the real Twin and Guardian: fix field-name differences in config (mapping), not in code
- [ ] Measure: messages per second handled, time added per message

### Week 4 (3–7 Nov)
- [ ] Report paragraph, tests clean-up, `INTERFACE.md` inputs section

## 6. Done when

- [ ] All 7 checks implemented, each with a unit test (valid + invalid case)
- [ ] The spec's HMAC test vector passes
- [ ] Interface tests IT-1 to IT-6 and IT-10, IT-11 of the spec pass with the mocks
- [ ] Handles at least **200 messages/s** with < 5 ms per message
- [ ] A restart of the Brain gets the city description, verdicts and quarantines back immediately (retained)

## 7. Suggested tools

`aiomqtt` or `paho-mqtt` · `pydantic` v2 · Python `hmac`, `hashlib` · `cryptography` (Ed25519) · a small time-limited set for seen ids (dict + cleanup every few seconds).

## 8. Who you work with

| Team | About |
|---|---|
| **Mock** | Their messages must pass your checks; give them the test key and envelope format early |
| **Graph** | They consume `reading`, `call`, `trust` |
| **Output** | They reuse your MQTT client and the signing format (same envelope, other direction) |
| **Team lead** | Topic names, keys and certificates agreed with the Twin and Guardian teams |

## 9. Risks and tips

- **Clock drift** on the real sensor boards will cause `E_STALE`. Log the time difference; ask the Twin team to use NTP.
- **Field names will differ** at the first joint run. Keep a mapping in config.
- Don't let one bad message crash the loop: catch everything per message.
