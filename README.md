# City Brain Convergence · triplet-05

TSYP14 technical challenge. One triplet (**triplet-05**), three layers of a smart city that work as one system:

| Layer | Question | Folder |
|---|---|---|
| **Twin** | What is happening in the city? (zones, sensors, actuators, units) | `twin/` |
| **City Brain** | What should we do? Detects incidents, decides, acts through the Twin's actuators | `brain/` |
| **Guardian** | Is this data telling the truth? Trust verdicts, containment | `guardian/` |

The City Brain runs alone with stand-ins of the other two layers (`brain/mocks/`, requirement R1).

## Start the city with one command

Requirements: Docker Desktop, Python 3.11+ (for the one-time setup), and [Ollama](https://ollama.com)
with `ollama pull qwen2.5-coder:7b` for the local LLM (optional: without it the rule fallback decides).

```bash
cd brain && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/
python tools/setup.py          # once: TLS certificates, HMAC keys, broker accounts (in brain/secrets/)
cd .. && docker compose up     # broker + every Brain module + mock Twin + mock Guardian
```

Open the live timeline on **http://localhost:8090** and play a Test Book scenario:

```bash
python scenarios/run.py B2     # real flood at the port: incident -> decision -> valve opened -> ack -> water falls
python scenarios/run.py --all  # every scenario, PASS/FAIL, one log file per run in logs/
```

Without Docker (recommended on the demo laptop, so the LLM runs next to the GPU):

```bash
docker compose up -d broker
cd brain && .venv/Scripts/python run.py --mocks      # add --mock-llm to run without Ollama
```

## Repository

| Path | Content |
|---|---|
| `brain/` | City Brain: `src/` modules, `mocks/`, `config/`, `contracts/schemas/`, `tests/`, `INTERFACE.md` (Interface Card) |
| `twin/`, `guardian/` | The other layers' folders (their teams) |
| `scenarios/` | One file + one trigger per Test Book scenario (`run.py`), R2 |
| `logs/` | One log file per scenario run, R2 |
| `TESTBOOK.md` | Test scenarios with expected outputs (report part 4) |
| `INTEGRATION.md` | How the three layers connect: diagram, protocol, one scenario end to end |
| `docs/` | Report and pitch, challenge requirements, team plan, design documents, reviews |
| `docker-compose.yml` | The whole city with one command |

Security in one line: TLS-only broker with one account per layer and a topic ACL, an HMAC-SHA256
envelope on every message (one key per pair of layers), freshness and replay checks, and Guardian
trust verdicts that weigh every decision. Details: `brain/INTERFACE.md`.
