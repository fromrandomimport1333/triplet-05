# Guardian — Interface Contract (v1.0)

> Guardian's job: **"Is this data telling the truth?"** It consumes Twin's sensor stream,
> scores trust, publishes signed verdicts and contains bad devices.
> Any change to this file must also be reflected in the root `INTEGRATION.md` (rule R4).

| Item | Value |
|---|---|
| Interface version | `1.0` (events, verdicts, control, timeline all carry `schema_version`) |
| Owner | Guardian team |
| Transport in | MQTT 3.1.1/5 over **mTLS** (port from `MQTT_PORT`, default 8883) |
| Transport out | MQTT (verdicts, control, timeline) + JSON-RPC 2.0 over **HTTPS/WSS with mTLS** |
| Addresses | Always from env (`MQTT_HOST`, `GUARDIAN_RPC_URL`, …) — never hard-coded |

---

## 1. What Guardian RECEIVES

### 1.1 Twin sensor events — MQTT `twin/events/<device_id>` (QoS 1)

Guardian subscribes to `twin/events/#` (prefix configurable via `TOPIC_TWIN_EVENTS`).

```json
{
  "schema_version": "1.0",
  "event_id": "wl-river-02-db7ebfe5ca1c",
  "seq": 13,
  "device_id": "wl-river-02",
  "sensor_type": "water_level",
  "value": 1.1975,
  "unit": "m",
  "ts": "2026-10-03T09:27:00.103346Z",
  "signature": "eb462b2aba22c3d9c8477445f1d43901cd4cb5a85d42b59d024c8b1031dd6b38"
}
```

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | major must be `1` (see §6) |
| `event_id` | string | globally unique, never reused |
| `seq` | int ≥ 0 | **strictly increasing per `device_id`** |
| `device_id` | string | stable device identifier |
| `sensor_type` | string | one of the table below (unknown types skip range checks) |
| `value` | number | finite |
| `unit` | string | must match `sensor_type` |
| `ts` | string | ISO-8601 **with timezone**, max 30 s in the future |
| `signature` | hex string | `HMAC-SHA256(TWIN_EVENT_HMAC_KEY, canonical_json(event without "signature"))` |

`canonical_json` = JSON with keys sorted, separators `,` and `:` (no spaces), UTF-8, no ASCII escaping
(Python: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

Physical envelopes used by the plausibility step:

| sensor_type | unit | min | max | max rate of change |
|---|---|---|---|---|
| water_level | m | 0 | 10 | 0.05 m/s |
| rainfall | mm/h | 0 | 300 | 5 /s |
| temperature | C | −30 | 60 | 0.5 /s |
| air_quality_pm25 | ug/m3 | 0 | 1000 | 20 /s |
| traffic_flow | veh/h | 0 | 10000 | 100 /s |
| power_load | kW | 0 | 5000 | 100 /s |

### 1.2 Twin quarantine acknowledgements — MQTT `twin/acks/quarantine/<device_id>` (QoS 1)

```json
{
  "schema_version": "1.0",
  "device_id": "wl-river-02",
  "action": "quarantine",
  "state": "quarantined",
  "control_sig": "sg6UHtTpDjnOBlC3O0OFi1SaRiaBf12722BYM+8Cm5Qt+kT/YRSUFp7q+pOGytY0k7vs3j4aIy1rEK1Je0MtBA==",
  "ack_at": "2026-10-03T15:24:38.416845Z"
}
```

### 1.3 JSON-RPC calls from City Brain / Twin / UI / admin — see §3

---

## 2. What Guardian SENDS

All outbound messages are **signed by Guardian** with Ed25519:
`sig.value = base64(Ed25519_sign(canonical_json(message without "sig")))`.
Public key: `GET /.well-known/guardian-verdict-key` (PEM). `sig.kid` = first 16 hex chars of
`sha256(raw public key)` so consumers can detect key rotation.

### 2.1 Trust verdict — MQTT `guardian/verdicts/<device_id>` (QoS 1)

One verdict for **every** event received, including invalid ones.

```json
{
  "schema_version": "1.0",
  "type": "trust_verdict",
  "event_id": "wl-river-02-db7ebfe5ca1c",
  "verdict_id": "wl-river-02-db7ebfe5ca1c",
  "device_id": "wl-river-02",
  "claimed_device_id": "wl-river-02",
  "trust_score": 1.0,
  "verdict": "trusted",
  "reason": "anomaly: z=0.29 vs EWMA mean=1.201",
  "failed_step": null,
  "issued_at": "2026-10-03T15:24:50.109048Z",
  "sig": {"alg": "Ed25519", "kid": "2ec7fe7317eabc20",
          "value": "DyDpi8LYZ16uRbaGhirF95Ne8TW8vctJ1BYRXutrKUkK0ymTtVW/TGtW8B4irH2I1LpTJEiECBVDijLHlkwFDg=="}
}
```

Rejected example (fake flood, failed at step 3):

```json
{
  "schema_version": "1.0",
  "type": "trust_verdict",
  "event_id": "wl-river-02-16c59c5a2a71",
  "verdict_id": "wl-river-02-16c59c5a2a71",
  "device_id": "wl-river-02",
  "claimed_device_id": "wl-river-02",
  "trust_score": 0.0,
  "verdict": "rejected",
  "reason": "plausibility: water_level jumped 1.1975->8.5m in 5s (rate 1.461/s > max 0.05/s)",
  "failed_step": 3,
  "issued_at": "2026-10-03T15:24:50.109413Z",
  "sig": {"alg": "Ed25519", "kid": "2ec7fe7317eabc20",
          "value": "ccjFt5OuGN5YhuyyiNQHCss3XD8kpzazWlk1fonlN1FnNX3r2j26qOKBolIoSZ64dUqFPevWTlAWyxUfIHJlBQ=="}
}
```

| Field | Meaning |
|---|---|
| `verdict` | `trusted` (score ≥ 0.8) · `suspicious` (0.4 ≤ score < 0.8, **logged, never auto-blocked**) · `rejected` (< 0.4 or failed steps 1–3) |
| `failed_step` | `1` schema/signature · `2` replay · `3` plausibility · `5` device quarantined · `null` if not rejected |
| `device_id` | `_unattributed` when the event failed step 1 (an unauthenticated `device_id` is never trusted); the claimed value is in `claimed_device_id` |
| `verdict_id` | equals `event_id`, except replays/duplicates: `<event_id>#replay-<n>` (the original verdict is never overwritten) |

**Detection pipeline (fixed order, each step short-circuits):**
1. schema + Twin HMAC signature → `rejected`
2. replay (duplicate `event_id` in sliding window, or `seq` ≤ last seq of the device) → `rejected`
3. plausibility (range, unit, rate of change vs last accepted reading, future ts) → `rejected`
4. EWMA z-score per device (`score = 1` if z ≤ 2, else `1 − (z−2)/8`); identical value for ≥ `STUCK_WINDOW_S` (300 s) caps the score at 0.6
5. score → verdict; readings from a quarantined device are forced to `rejected` (`failed_step` 5)
6. sign → store `trust_verdicts` → update `device_trust_scores` → publish → `audit_log` (hash-chained)

### 2.2 Device control (quarantine / release) — MQTT `guardian/control/<device_id>` (QoS 1, **RETAINED**)

```json
{
  "schema_version": "1.0",
  "type": "device_control",
  "action": "quarantine",
  "device_id": "wl-river-02",
  "reason": "3 consecutive rejected verdicts",
  "actor": "guardian",
  "issued_at": "2026-10-03T15:24:38.416599Z",
  "sig": {"alg": "Ed25519", "kid": "f51e62059a82a194",
          "value": "sg6UHtTpDjnOBlC3O0OFi1SaRiaBf12722BYM+8Cm5Qt+kT/YRSUFp7q+pOGytY0k7vs3j4aIy1rEK1Je0MtBA=="}
}
```

`action` is `quarantine` or `release`. For a release, `actor` is `admin:<sub>` of the admin token.

### 2.3 Rollback advisory — MQTT `guardian/verdicts/rollback/<device_id>` (QoS 1)

Sent right after a quarantine. Lists the device's readings accepted (`trusted`/`suspicious`)
during the last `ROLLBACK_WINDOW_S` (600 s) — City Brain decides whether to revert actions
it took on them (Guardian never acts inside Brain's territory).

```json
{
  "schema_version": "1.0",
  "type": "rollback_advisory",
  "device_id": "wl-river-02",
  "window_start": "2026-10-03T15:14:38.416866Z",
  "event_ids": ["wl-river-02-db7ebfe5ca1c"],
  "issued_at": "2026-10-03T15:24:38.416880Z",
  "sig": {"alg": "Ed25519", "kid": "f51e62059a82a194",
          "value": "TUBnW6O85qM2L/DEw+1iS+N6p6ovof101ifDRh5ih9GQiLsSwqAaYzMRjabYt6k/w46Cz7qSaC1TnWYe/oSyAA=="}
}
```

### 2.4 Shared timeline contribution (R3) — MQTT `triplet/timeline/guardian` (QoS 0) and `GET /timeline`

UI-framework agnostic: plain JSON entries; the shared screen can subscribe (push) or poll (pull).
Guardian emits entries for every non-trusted verdict, every quarantine and every release.

```json
{
  "schema_version": "1.0",
  "layer": "guardian",
  "id": 17,
  "ts": "2026-10-03T15:24:38.422054Z",
  "kind": "verdict_rejected",
  "device_id": "wl-river-02",
  "severity": "high",
  "summary": "plausibility: water_level jumped 1.1975->8.5m in 5s (rate 1.461/s > max 0.05/s)",
  "ref": "wl-river-02-16c59c5a2a71"
}
```

`kind` ∈ `verdict_suspicious` (severity `warning`), `verdict_rejected` (`high`), `quarantine` (`critical`), `release` (`info`).
Pull: `GET /timeline?since=<ISO ts>&limit=<1..1000>` with a JWT whose `tool` is `timeline`
→ `{"layer": "guardian", "entries": [ ... ]}`.

---

## 3. JSON-RPC 2.0 tools — `POST /rpc` (HTTPS) or `/rpc/ws` (WSS)

| Method | Params | Result | Allowed roles |
|---|---|---|---|
| `get_trust_verdict` | `{"event_id": str}` | `{event_id, trust_score, verdict, reason, sig}` | brain, admin |
| `get_device_status` | `{"device_id": str}` | `{"status": "trusted"\|"suspicious"\|"quarantined"}` | brain, twin, ui, admin |
| `release_device` | `{"device_id": str}` | `{"status": ...}` — always written to `audit_log` | **admin only** |
| `health` | `{}` | `{"status": "ok"\|"degraded"}` | all |

`get_trust_verdict` result is signed (extra `sig` member, same scheme as §2). Unknown devices
report `trusted` (no evidence against them). After release a device is `suspicious` (probation) until its next trusted reading.

Request / response:

```http
POST /rpc HTTP/1.1
Authorization: Bearer eyJhbGciOiJIUzI1NiIsImtpZCI6ImJyYWluIiwidHlwIjoiSldUIn0.eyJpc3MiOiJicmFpbiIsInN1YiI6ImJyYWluIiwiYXVkIjoiZ3VhcmRpYW4iLCJ0b29sIjoiZ2V0X3RydXN0X3ZlcmRpY3QiLCJpYXQiOjE3OTEwNDEwOTAsImV4cCI6MTc5MTA0MTE1MCwianRpIjoiNWM2ZWE2ZGJkZTBiNDlmZjk3NzY5N2VlOGJmZDliNzcifQ.R3leV5_WpEn4Fyo26Dj7vuGutadN28Xj8VsaDqvqg_4
Content-Type: application/json

{"jsonrpc": "2.0", "method": "get_trust_verdict", "params": {"event_id": "wl-river-02-db7ebfe5ca1c"}, "id": 1}
```

```json
{"jsonrpc": "2.0", "id": 1, "result": {
  "event_id": "wl-river-02-db7ebfe5ca1c", "trust_score": 1.0, "verdict": "trusted",
  "reason": "anomaly: z=0.29 vs EWMA mean=1.201",
  "sig": {"alg": "Ed25519", "kid": "2ec7fe7317eabc20",
          "value": "vZkYnztW1Hb9W3Mdp3KcLxIym7MLw6gmT6G+xBACI2+e4Ve8nCZE8vDUMUtjtH2nLD38TbY8+aqusGOqEnnqBw=="}}}
```

Error example (City Brain token calling `release_device`):

```json
{"jsonrpc": "2.0", "id": 6, "error": {"code": -32003, "message": "forbidden",
  "data": "role 'brain' may not call 'release_device'"}}
```

| Code | Meaning |
|---|---|
| −32700 | parse error |
| −32600 | invalid request (not JSON-RPC 2.0, params not an object) |
| −32601 | unknown method |
| −32602 | invalid params |
| −32001 | unauthorized (missing/invalid/expired/reused token, cert mismatch) |
| −32003 | forbidden (role or token scope does not cover this tool) |
| −32004 | not found (no verdict for this `event_id` yet — retry briefly) |
| −32603 | internal error (details never leaked) |

**WebSocket / batch:** the token travels in `params._jwt` (one token per call), e.g.
`{"jsonrpc":"2.0","method":"health","params":{"_jwt":"<token>"},"id":9}`.
Batches over HTTP are supported; each item needs its own `params._jwt`.

Unauthenticated helper endpoints (still behind mTLS): `GET /healthz` (liveness), `GET /.well-known/guardian-verdict-key`.

---

## 4. Authentication

### 4.1 mTLS (all inbound and outbound connections)
* One triplet CA. Every service presents a certificate signed by it; servers require client certificates (`CERT_REQUIRED`). TLS ≥ 1.2.
* Client certificate CNs: `brain.triplet`, `twin.triplet`, `ui.triplet`, `admin.triplet`. Guardian's server cert: `guardian` (SAN `guardian`, `localhost`); MQTT client cert: `guardian-client.triplet`.
* On HTTP, Guardian additionally binds the certificate to the token: the CN must be in `ALLOWED_CLIENT_CNS` and map to the JWT issuer (a stolen admin token is useless on Brain's certificate).
* Limitation: on the WebSocket endpoint the CA check is enforced at handshake, but the CN↔issuer binding is only enforced on HTTP (uvicorn does not expose the peer cert to WebSocket handlers).
* Dev certificates: `python -m mocks.gen_dev_certs --out certs` (never use in production).

### 4.2 JWT, signed per tool call
* HS256, header `kid` = issuer = role (`brain`, `twin`, `ui`, `admin`). **Each role has its own secret** (`GUARDIAN_JWT_KEYS`); the role is derived from the key that verifies the signature, never from a claim.
* Required claims: `iss` (= `kid`), `aud` = `guardian`, `tool` (= the JSON-RPC method or `timeline`), `iat`, `exp` (lifetime ≤ `JWT_MAX_TTL_S`, 120 s), `jti` (single-use; reuse → −32001).
* Decoded example: header `{"alg":"HS256","kid":"brain","typ":"JWT"}`, payload
  `{"iss":"brain","sub":"brain","aud":"guardian","tool":"get_trust_verdict","iat":1791041090,"exp":1791041150,"jti":"e3228a9c797f4e5c8c0ee5060b583bf0"}`.
* Python helper for partners: `src.core.security.issue_tool_token(secret, issuer, tool)`.

### 4.3 Message signatures
* Twin → Guardian: HMAC-SHA256 on every event (§1.1).
* Guardian → everyone: Ed25519 on every verdict, control message, rollback advisory and `get_trust_verdict` result (§2). **Consumers must drop any Guardian message whose signature does not verify.**

---

## 5. Quarantine contract — what Twin MUST do

This is Guardian's only cross-layer write into Twin's territory. Guardian never touches
Twin's code or state directly; it publishes a signed instruction and Twin enforces it.

**Trigger:** 3 consecutive counted `rejected` verdicts for the same `device_id` (`QUARANTINE_THRESHOLD`).
Counted = failed plausibility/anomaly. **Not counted:** signature/schema failures and replays —
an attacker without the device's key, or replaying its old traffic, must not be able to get a healthy device quarantined.
Override with `QUARANTINE_COUNT_UNAUTHENTICATED=true` if the triplet prefers strictness.

On a message on `guardian/control/<device_id>`, Twin MUST:

1. **Verify** the Ed25519 `sig` with Guardian's public key. If verification fails, or `type` ≠ `device_control`: ignore the message and log it as a security event. Never act on an unsigned control message.
2. **Subscribe with a persistent session to `guardian/control/#`** and honour **retained** messages on (re)connect — the retained message is the source of truth for the device's state.
3. On `action = "quarantine"`, within **2 s**:
   * mark the device `quarantined`;
   * **refuse every actuator command** targeting it (from City Brain or anyone else) with error `{"code": "DEVICE_QUARANTINED", "message": "<device_id> is quarantined by Guardian"}`, and do not execute queued commands for it;
   * **keep publishing its telemetry** on `twin/events/<device_id>` (Guardian keeps rejecting it with `failed_step` 5 — this keeps forensic evidence flowing);
   * other devices are unaffected.
4. On `action = "release"`: re-enable commands for the device.
5. **Acknowledge** every applied control message on `twin/acks/quarantine/<device_id>` (format §1.2) with `control_sig` = the `sig.value` received.
6. Be **idempotent**: receiving the same message twice changes nothing.
7. Never un-quarantine a device by itself. The only exit is Guardian's `release_device` (admin, audit-logged) which publishes the `release` message.

Reference implementation: `mocks/twin_mock.py` (`on_control`, `execute_command`), verified by Test Book scenario G5.

City Brain MUST: never act on a reading whose verdict is `rejected` or missing (G4); treat `suspicious` as actionable-but-flagged; on a rollback advisory, re-assess actions taken on the listed `event_ids`.

---

## 6. Versioning policy

* Every message carries `schema_version` = `MAJOR.MINOR`.
* **MINOR** bump = additive only (new optional fields). Consumers MUST ignore unknown fields. Guardian accepts any `1.x` event.
* **MAJOR** bump = breaking change. Guardian rejects events with an unsupported major (`reason: "schema: unsupported major version …"`).
* A breaking change is announced in root `INTEGRATION.md` at least **5 days before** it ships, and Guardian accepts both majors in parallel during that window.
* JSON-RPC method names and param names are frozen for v1; new tools may be added (minor).
* Every change to this file is mirrored in `INTEGRATION.md` §Guardian in the same PR (R4).

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-10-03 | Initial Phase 1 interface |
| 1.2 | 2026-11-07 | Added section 8: Brain protocol profile (integration kit), behind BRAIN_PROTOCOL_ENABLED. Sections 1-7 unchanged. |
| 1.3 | 2026-11-07 | Section 8: verdict refresh every 20 s while the Twin is silent (30 s liveness rule); frozen sensor after 8 identical readings. Message formats unchanged. |

---

## 7. Storage (for reviewers)

TimescaleDB tables exactly as specified: `device_trust_scores`, `trust_verdicts` (hypertable on `timestamp`),
`quarantine_list`, `audit_log` (append-only via triggers that block UPDATE/DELETE/TRUNCATE; hash chain
`this_hash = sha256(prev_hash + canonical_json(row))`, genesis `prev_hash` = 64 × `0`; the hashed row content is
`log_id, timestamp, action, device_id, details, prev_hash`).
Note: TimescaleDB requires the partition column in every unique index, so the physical primary key of
`trust_verdicts` is `(event_id, timestamp)`; Guardian guarantees `event_id` uniqueness itself (replays stored as `<event_id>#replay-<n>`).

## 8. Brain protocol profile — Interface Card (R1)

Filled from the City Brain team's template (`templates/guardian-INTERFACE.md` of the integration kit).
Enabled with `BRAIN_PROTOCOL_ENABLED=true`; sections 1-7 (native protocol) keep working unchanged.
Code: `src/adapter/` (envelope, registry, actuators, brain). Body schemas: the kit's
`external-*.schema.json`, copied unmodified in `guardian/contracts/kit-schemas/` and enforced by
`tests/brain/test_contract.py`.

### Transport and authentication

| | |
|---|---|
| **Protocol** | MQTT 3.1.1 over **TLS** (port from `MQTT_PORT`, 8883 on the Brain broker), broker certificate checked against the triplet CA (`MQTT_CA_FILE` = ca.crt). No client certificate (`MQTT_CERT_FILE` empty). |
| **Broker account** | `guardian` (`MQTT_USERNAME`) + password file (`MQTT_PASSWORD_FILE`). Writes only `guardian/#`; reads `city/#` (we subscribe only `city/+/sensor/+/+`), `twin/#`, `brain/cmd/#`, `brain/alert`, `brain/error`, `guardian/control`. Two clients, each with its own id (`MQTT_CLIENT_ID_PUB`, `MQTT_CLIENT_ID_SUB`, default `<MQTT_CLIENT_ID>-pub-<random>`). |
| **Message signature** | Envelope `{"v": 1, "body": "<JSON text>", "hmac": "<hex>"}` (single reader: the Brain). HMAC-SHA256 over the exact UTF-8 body text with guardian-brain.key: the file's text, whitespace stripped, used as UTF-8 bytes. Ed25519 is not used on this profile (our native messages stay Ed25519-signed). The kit test vector (handover 5.3) is a unit test. |
| **Verifying what we receive** | Twin readings, twin/meta, acks: `hmacs.guardian` with twin-guardian.key. brain/cmd and brain/alert: `hmacs.guardian` with guardian-brain.key. A reading with a bad or missing HMAC gets **rejected / bad_hmac** on `guardian/verdict/<sensor id from the topic>`, **not retained** (the retained real verdict is never overwritten), `correlation_id` = the reading id when readable, and is **never** counted toward quarantine (an attacker without the key must not be able to isolate a healthy sensor). If the topic id fails `^[a-z0-9][a-z0-9-]*$`, nothing is sent to the Brain (native verdict + audit only). |
| **Freshness / ids** | `timestamp` = ISO 8601 UTC with exactly milliseconds; clock from the host (sync with NTP). Every id is `gd-<GUARDIAN_RUN_ID>-<counter>` (random run id per start if unset). Inputs older/newer than `CLOCK_SKEW_S` (10 s) are audited (`reading_stale`) and not judged. |
| **Liveness** | Last Will `guardian/status` offline (retained, sealed) set at connect; `online` (retained) published right after each connection. One verdict per received reading; if no verdict went out for `VERDICT_REFRESH_S` (20 s, e.g. the Twin is silent), the current verdict of every known sensor is re-sent (new id and timestamp, same verdict, reason, score and `correlation_id`, `details` starting with `refresh`), so the Brain never sees 30 s without a verdict. |

### Inputs (Twin and Brain → Guardian)

| Topic | From | What the Guardian does with it |
|---|---|---|
| `twin/meta` (retained) | Twin | Learns zones, sensors (type, unit, min, max), actuators and units. If the envelope has `hmacs.guardian` it is verified (invalid ⇒ ignored + audit `meta_rejected`); without a guardian tag it is accepted and audited `meta_loaded_unverified`. A new meta id resets the per-device seq table (Twin restart). |
| `city/{zone}/sensor/{type}/{sensor_id}` | Twin | Judges each reading: HMAC, topic/body agreement, sensor known in twin/meta, freshness, replay (id within the window, seq strictly increasing per sensor), range (meta min/max), implausible jump (per-tick step and rate, water_level only by default), frozen value (`FROZEN_COUNT` = 8 identical consecutive readings ⇒ *suspicious* / `frozen`, never quarantined), EWMA anomaly (can only lower to *suspicious*). Units are converted to the sensor's meta unit (aliases and cm/mm, vehicles/min ↔ veh/h, km/h ↔ m/s, W/kW/MW). |
| `city/{zone}/call` | Twin | Not used: not subscribed, and the router never sends a non-sensor topic to the pipeline (B7: no verdict for calls). |
| `twin/actuator/{id}`, `twin/unit/{id}` | Twin | Checks acknowledgements against the commands seen: unknown `correlation_id`, ack for another target, and no ack after `ACK_TIMEOUT_S` + `ACK_GRACE_S` (4 + 4 s) each count one inconsistency; `ACK_QUARANTINE_THRESHOLD` (2) on one device ⇒ quarantine, reason `inconsistent_ack`. Retained snapshots and status changes without `result`/`correlation_id` are not acks. |
| `brain/cmd/{target_id}` | Brain | Records `id → (target, action, time)`. A repeat of the same id (Brain retry) is not an anomaly. Commands without a valid `hmacs.guardian` are audited `command_unverified` and not recorded. |
| `brain/alert` | Brain | `actuator_no_effect` / `actuator_not_responding` count one inconsistency for `target_id` (`ALERT_COUNTS_AS_INCONSISTENCY`). A `target_id` not in twin/meta is ignored + audited. |
| `brain/error` | Brain | When `message_id` is one of ours, audited `brain_refused_our_message` (code, detail). |
| `guardian/control` (optional) | tester | Sealed with guardian-brain.key, fresh (±10 s), id not seen in the last 60 s, not retained. Supported: `{"op":"reset"}` — clears baselines, replay cache, streaks, ack table, and releases every quarantined device (publishes `released`, reason `reset`). Other ops are audited `control_unsupported`. |

### Outputs (Guardian → Brain)

Every body has `id`, `timestamp`, `source: "guardian"` (left out of the examples after the first). Topic and body ids always agree.

| Topic | QoS | Ret. | Example body |
|---|---|---|---|
| `guardian/verdict/{sensor_id}` | 1 | yes (no for `bad_hmac` / `replay`) | `{"id":"gd-r1-000613","timestamp":"2026-11-14T10:00:10.004Z","source":"guardian","correlation_id":"tw-r1-000182","sensor_id":"water-01","verdict":"rejected","reason":"implausible_jump","trust_score":0.0,"details":"plausibility: water_level jumped 0.385->1.8m (step 1.415 > max 1.0 per tick)"}` |
| `guardian/quarantine/{device_id}` | 1 | yes | `{"device_id":"water-01","state":"quarantined","reason":"implausible_jump"}` · `{"device_id":"valve-01","state":"quarantined","reason":"inconsistent_ack"}` · lifted with `"state":"released"` (reason `reset` or `manual_release`) |
| `guardian/status` | 1 | yes | `{"status":"online"}` (Last Will `offline`) |

- `verdict`: trusted · suspicious · rejected; `trust_score` 0..1; `correlation_id` = the id of the reading judged.
- `reason` values we send: `consistent`, `out_of_range`, `implausible_jump`, `frozen`, plus ours: `anomaly` (suspicious only), `bad_hmac`, `unit_mismatch`, `quarantined` (reading from a quarantined sensor), `replay` (only if `BRAIN_PUBLISH_REPLAY_VERDICTS=true`), and `inconsistent_ack` on quarantines. `details` carries the native reason text (≤ 280 chars).
- The quarantine notice is published right **after** the verdict that caused it.
- Native outputs (sections 2.1-2.4) are still published, all inside `guardian/#`; the timeline defaults to `guardian/timeline` in this profile.

**Organizer example T5** (local run, scenario G6): fake 1.8 m from t = 10 s ⇒ `rejected / implausible_jump` at t = 10 s (first fake reading), `guardian/quarantine/water-01 quarantined` at t = 12 s (third consecutive reject).

### Pointing the Guardian at another partner (configuration only)

| What changes | Where |
|---|---|
| Broker host, port, client ids | `guardian/.env`: `MQTT_HOST`, `MQTT_PORT`, `MQTT_CLIENT_ID_PUB`, `MQTT_CLIENT_ID_SUB` |
| CA file, account, password file, HMAC key files (twin-guardian, guardian-brain) | `guardian/.env`: `MQTT_CA_FILE`, `MQTT_USERNAME`, `MQTT_PASSWORD_FILE`, `TWIN_GUARDIAN_KEY_FILE`, `GUARDIAN_BRAIN_KEY_FILE`, `HMAC_TAG` (files kept outside the repo) |
| Topic names | `guardian/.env`: `TOPIC_*` (templates with `{zone_id}`, `{type}`, `{sensor_id}`…) |
| Thresholds (max step / rate / noise per sensor type, quarantine and ack thresholds, clock skew, frozen count, refresh period) | `guardian/config/sensor_limits.brain.json` (`SENSOR_LIMITS_FILE`) and `guardian/.env` (`QUARANTINE_THRESHOLD`, `ACK_*`, `CLOCK_SKEW_S`, `FROZEN_COUNT`, `VERDICT_REFRESH_S`, `ANOMALY_*`) |
| City | learned from `twin/meta`, never configured |

Nothing partner-specific is in the code.

### Run alone (R1)

```bash
cd guardian && pip install -r requirements.txt
python -m pytest -q tests                              # includes tests/brain (Brain profile, no broker)
bash ../scenarios/run_guardian_scenarios.sh local      # G1-G9, G6-G9 = Brain profile with stand-ins
# against the Brain team's stand-ins (their repo: python run.py --mocks twin):
BRAIN_PROTOCOL_ENABLED=true MQTT_HOST=<brain-ip> MQTT_USERNAME=guardian \
MQTT_PASSWORD_FILE=~/triplet-secrets/guardian-password.txt MQTT_CA_FILE=~/triplet-secrets/ca.crt \
TWIN_GUARDIAN_KEY_FILE=~/triplet-secrets/twin-guardian.key GUARDIAN_BRAIN_KEY_FILE=~/triplet-secrets/guardian-brain.key \
SENSOR_LIMITS_FILE=config/sensor_limits.brain.json ANOMALY_MIN_SCORE=0.4 REQUIRE_MTLS=false python -m src.main
```

### Calibration (B1, B2, B3 = T5)

The Brain team's mock Twin was **not available** to us; nothing below was measured on it. Values
are derived from the figures written in the kit and checked on our synthetic Twin
(`mocks/brain_twin_sim.py`: 36 sensors, one reading per sensor per second, noise amplitudes assumed).

| Setting | Value | Why |
|---|---|---|
| Reading period | 1 s per sensor | Test Book B1 (Twin: "readings of all 36 sensors every second"); to be confirmed on the real mock Twin. |
| `water_level.max_step` | 1.0 m per tick | T5 fake step is 1.4 m (0.4 → 1.8); a real flood rises ~0.15 m/s (B2) and the valve lowers 0.3 m at once (B2). Margin on both sides. The step rule applies while the last accepted reading is < `STEP_WINDOW_S` (30 s) old. |
| `water_level.max_rate_per_s` | 0.8 m/s (was 0.05 m/s in the native table) | 0.05 m/s would reject the B2 rise (0.15 m/s). 0.8 > real rise + noise, < fake 1.4 m/s. Native profile keeps 0.05. |
| Other types: step/rate | off | B11 says power/particulate steps are real events; only meta min/max apply. |
| `noise_floor` per type | water 0.1 m, speed 5 km/h, density 5 veh/km, flow 3 veh/min, power 50 kW, air 10 ug/m3 | floor of the EWMA std so smooth rush-hour or flood trends never score as anomalies (B1: no suspicious). |
| `ANOMALY_MIN_SCORE` | 0.4 | the anomaly rule alone can only make a reading *suspicious*, never *rejected* (B11: real steps must not be rejected). |
| `FROZEN_COUNT` | 8 identical readings | at 1 reading/s a frozen sensor is flagged after 8 s, well inside a 60-90 s Test Book run; the synthetic Twin's noisy sensors never repeat a value 8 times (B1, B2 and fire runs: no `frozen`). *suspicious* only: a frozen value is not proof of an attack. |
| `VERDICT_REFRESH_S` | 20 s | 10 s margin under the Brain's 30 s rule. |
| `QUARANTINE_THRESHOLD` | 3 consecutive rejects | at 1 reading/s the first fake (t = 10 s) is rejected at once and the quarantine comes at t = 12 s (< 20 s, T5); one isolated glitch (or two) never quarantines a healthy sensor, since any accepted reading resets the streak. With a 2 s period it would be t = 14 s, still inside 20 s. |

### Assumptions (the kit is silent; each one is configurable)

1. Readings without `hmacs.guardian` but with a single `hmac` are checked against that `hmac` with twin-guardian.key (fails ⇒ `bad_hmac`).
2. `guardian/control` is sealed with **guardian-brain.key** (`hmac` or `hmacs.guardian`), carries `id` and `timestamp`, and is not retained.
3. A bad-HMAC verdict carries `correlation_id` only when the unverified body has a usable `id`.
4. Twin restart: a twin/meta with a **new id** resets the per-sensor seq table (`SEQ_RESET_ON_NEW_META`); without a new meta, a restarted seq is rejected as replay (not counted toward quarantine) until it passes the old value.
5. Readings without an integer `seq`: our own counter is used, so replay detection is id-based only.
6. Malformed readings, unknown sensors, topic/body disagreement and stale timestamps get an audit entry, no Brain verdict (the Brain refuses them itself with E_FORMAT / E_UNKNOWN_ID / E_STALE).
7. Duplicate readings are rejected and audited but not sent to the Brain (`BRAIN_PUBLISH_REPLAY_VERDICTS=false`); when enabled they go out not retained.
8. An ack is a message with `result` or `correlation_id`; acks arriving during `ACK_STARTUP_GRACE_S` (10 s) after start or reset are not judged (commands sent before we connected).
9. `brain/error` bodies are audited even when their signature cannot be verified (we do not know which key seals them).
10. A refreshed verdict counts as a "live verdict" for the Brain: it keeps the `correlation_id` of the last reading judged (no new reading exists). Set `VERDICT_REFRESH_S=0` to turn it off.
11. A frozen sensor is reported *suspicious* (reason `frozen`), not rejected, and never counts toward quarantine.

### Known limitations

- **Twin cannot read our quarantines.** Under the current ACL the twin account reads `brain/cmd/#`, `brain/status`, `brain/error`, `twin/control` only, so it cannot see `guardian/quarantine/#` (nor our native `guardian/control/#`). We do not work around the ACL. **Request to the Brain team:** add to `brain/broker/acl` for user `twin`: `topic read guardian/quarantine/#` and `topic read guardian/control/#`.
- `guardian/control` ops `mode`, `offline`, `online` (Brain mocks `brain/mocks/guardian/`) are **not implemented**: we did not have that folder. B5 and B8 expect quarantine notices for unit-ambulance-02 / valve-01 at the start through those ops, so B5/B8 will not get them from the real Guardian until the formats are shared.
- Before the first reading of a run there is nothing to refresh, so the 30 s rule is only kept once readings have flowed.
- Nothing was run against the real broker, Brain or mock Twin (see the validation report).
