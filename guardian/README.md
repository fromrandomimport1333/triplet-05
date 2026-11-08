# Guardian (TSYP14 City Brain Convergence)

Guardian answers one question for the triplet: **"Is this data telling the truth?"**
It consumes Twin's sensor stream, scores trust, publishes signed verdicts and quarantines bad devices.
Contract for partners: [`INTERFACE.md`](INTERFACE.md).

## Two protocol profiles

| Profile | Switch | Contract |
|---|---|---|
| Native (Phase 1 design) | default | INTERFACE.md sections 1-7 |
| City Brain integration kit | `BRAIN_PROTOCOL_ENABLED=true` | INTERFACE.md section 8 (MQTT TLS + account, HMAC envelopes, `guardian/verdict|quarantine|status`) |

Connect to the Brain team's broker (secrets kept outside the repo):

```bash
cd guardian
cp .env.example .env      # then set BRAIN_PROTOCOL_ENABLED=true, MQTT_HOST, MQTT_USERNAME=guardian,
                          # MQTT_PASSWORD_FILE, MQTT_CA_FILE (ca.crt), empty MQTT_CERT_FILE/MQTT_KEY_FILE,
                          # TWIN_GUARDIAN_KEY_FILE, GUARDIAN_BRAIN_KEY_FILE, SENSOR_LIMITS_FILE, ANOMALY_MIN_SCORE=0.4
python <kit>/tools/partner_check.py --host <brain-ip> --layer guardian --password-file ... --key-file guardian-brain.key --ca ca.crt
python -m src.main
```

## Prerequisites
* Docker 24+ with Docker Compose v2 — for the full stack (TimescaleDB, MQTT broker, mocks)
* or Python 3.12 — for tests and the Test Book in local mode (no infrastructure needed)

## Option A — Python only, under 2 minutes (no Docker)
```bash
cd guardian
python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q tests                                  # unit + contract + adversarial
bash ../scenarios/run_guardian_scenarios.sh local          # Test Book G1-G11 → logs/
```
Local mode runs the real FastAPI app and the real pipeline with an in-memory store, an in-process
MQTT bus and `mocks/` for Twin and City Brain.

## Option B — full standalone stack with Docker, under 5 minutes
From the **repo root**:
```bash
cp guardian/.env.example guardian/.env
docker compose --profile guardian up --build -d            # certs, TimescaleDB, mosquitto, Guardian, Twin mock
docker compose --profile guardian ps                       # guardian should be "healthy"
docker compose --profile guardian --profile guardian-test run --rm guardian-scenarios   # G1-G5 live → logs/
```
What starts: `guardian-certs` (one-shot dev CA + mTLS certs), `guardian-db` (TimescaleDB),
`guardian-mqtt` (mosquitto, mTLS on 8883, stands in for Twin's broker), `guardian` (API on
`https://localhost:8443`), `guardian-twin-mock` (continuous normal traffic).

Call the API from your machine (mTLS + per-call JWT):
```bash
docker compose --profile guardian cp guardian:/certs ./guardian/certs
python - <<'PY'
import sys; sys.path.insert(0, "guardian")
from mocks.brain_mock import HttpRpc
rpc = HttpRpc("https://localhost:8443", "dev-brain-secret-change-me-0123456789abcdef", "brain",
              ca="guardian/certs/ca.pem", cert="guardian/certs/brain.pem", key="guardian/certs/brain.key")
print(rpc.call("health"))
print(rpc.call("get_device_status", {"device_id": "wl-demo-00"}))
PY
```
Stop: `docker compose --profile guardian down` (add `-v` to wipe the DB and certs).

## Environment variables
All are in [`.env.example`](.env.example) with comments. Key ones:

| Variable | Purpose |
|---|---|
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_*_FILE` | broker carrying Twin's events (mTLS) |
| `TOPIC_TWIN_EVENTS`, `TOPIC_VERDICTS`, `TOPIC_CONTROL`, `TOPIC_TIMELINE` | topic prefixes |
| `DATABASE_URL` | PostgreSQL/TimescaleDB DSN (empty ⇒ in-memory, tests only) |
| `GUARDIAN_JWT_KEYS` | `role=secret` list (brain, twin, ui, admin) |
| `TWIN_EVENT_HMAC_KEY` | shared with Twin to verify event signatures |
| `VERDICT_SIGNING_KEY_FILE` | Guardian's Ed25519 key (created on first start) |
| `TLS_CA_FILE`, `TLS_CERT_FILE`, `TLS_KEY_FILE`, `REQUIRE_MTLS`, `ALLOWED_CLIENT_CNS` | API mTLS |
| `TWIN_BASE_URL`, `BRAIN_BASE_URL` | partner addresses |
| `STUCK_WINDOW_S`, `QUARANTINE_THRESHOLD`, `QUARANTINE_COUNT_UNAUTHENTICATED`, `REPLAY_*`, `ROLLBACK_WINDOW_S` | detection tuning |

## Mocks vs real partners
| | Mocks (default) | Real partners |
|---|---|---|
| Twin events | `guardian-twin-mock` / `mocks/twin_mock.py` | set `MQTT_HOST`/`MQTT_PORT` to Twin's broker, use the triplet CA certs |
| City Brain | `mocks/brain_mock.py` | Brain calls `GUARDIAN_RPC_URL` with its own cert + `brain` JWT secret |
| Quarantine enforcement | `TwinMock.on_control` | Twin implements INTERFACE.md §5 |

Only `guardian/.env` changes — no code changes. Stop the mock with
`docker compose --profile guardian stop guardian-twin-mock`.

## Tests
```bash
cd guardian && python -m pytest -q tests
```
* `tests/unit` — each pipeline step 1–6, containment (auto-quarantine after 3 rejects), audit-chain tamper proof, JWT scopes
* `tests/contract` — JSON-RPC contract vs `mocks/`; `test_pg_store.py` runs against TimescaleDB when `GUARDIAN_TEST_DATABASE_URL` is set (CI does this)
* `tests/adversarial` — spoofing, replay, MITM, forged control messages, and a real TLS server proving mTLS

CI: `.github/workflows/guardian.yml` (runs only on `guardian/**` and Guardian scenarios).

## Layout
```
src/main.py            FastAPI entrypoint            src/api/        JSON-RPC tools + routes
src/core/              JWT/mTLS, signing, timeline   src/mqtt/       subscriber / publisher
src/db/                schema + stores               src/detection/  pipeline steps 1-6
src/containment/       isolate, quarantine, rollback mocks/          twin, brain, dev certs, local stack
```
