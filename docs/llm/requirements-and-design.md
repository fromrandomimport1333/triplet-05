# LLM module: requirements, design and open points

Scope: `brain/src/llm/` and `brain/mocks/mock_llm/` (team plan `03-llm`). How to run it is in
`brain/README.md` (LLM module); the report paragraph is `docs/report/brain-llm.md`.

## 1. Requirements

| Source | Requirement | How it is met |
|---|---|---|
| 03-llm §1 | Propose actions when an incident opens or changes; only propose | Publishes `brain/internal/proposal` (`origin: llm`); math scores, output applies safety rules |
| 03-llm §1 | City keeps working if the LLM is slow or offline | Rule fallback (math); this module publishes `llm_timeout` and `llm-status offline` so the fallback is used at once |
| 03-llm §3 | In: `incident`, `GET /context/{id}`, `meta` (allowed actions) | `incident`, `meta`, plus `call`, `ack`, `quarantine/+` to build context locally; graph context optional |
| 03-llm §3 | Out: proposal, `llm_proposal` / `llm_timeout` / `llm_invalid_output`, `llm-status` with Last Will | Yes; plus `llm_skipped`, `llm_online`, `llm_offline` |
| 03-llm §4.1 | Prompt < 1,500 chars, no raw streams, fixed JSON output, allowed actions only, temperature 0, prompt version in config, every prompt and answer logged | `context.max_chars`, structured output with a per-incident JSON Schema, `prompt.version`, `logs/llm.jsonl` |
| 03-llm §4.2 | Call text in a labelled data block; strict validation (known IDs, allowed actions, ≤ 3 actions) | `<call_text>` block with `<`/`>` neutralised; `validate.py`; **plus** relevant device types per incident type |
| 03-llm §4.3 | Answer within 8 s (configurable); drop the old request when a newer update arrives | Hard deadline from reception, streamed answer closed at the deadline; latest version wins; `closed` cancels |
| 03-llm §5 | Benchmark 2–3 small models (3B–8B, 4-bit) on the device; ≤ 8 s, ≥ 95 % valid JSON, correct on B2/B5/B11, resists B10 | `python -m src.llm.bench` on `tests/llm/cases.yaml`; results in the report paragraph |
| 03-llm §6 | Mock LLM: `scripted`, `slow` (15 s), `offline`, `invalid` | `python -m mocks.mock_llm <mode>` |
| 03-llm §8 | Runs fully offline | Ollama on the laptop; no network call besides the local broker and Ollama |
| Phase 1 §10 | Don't rely on venue Wi-Fi; local fallback for any cloud use | Local model only |
| 00 §5 | Python 3.11+, config in YAML, JSON-lines logs with `correlation_id`, module runs alone | Yes; no new dependency (stdlib HTTP) |

**Device (decided):** the demo laptop is the edge device: NVIDIA RTX 5060 Laptop GPU (8 GB
VRAM), 32 GB RAM, Windows 11, Ollama 0.40.6.

## 2. Architecture

```
            brain/internal/meta, quarantine/+, ack, call (retained / live)
                              │
                              ▼
 brain/internal/incident ─▶ LlmService.handle ──▶ queue (latest version per incident)
                                                      │ worker thread
                                                      ▼
                         CityState.build ─▶ (+ GraphContextClient, optional) ─▶ Context
                                                      │  targets = zone + neighbours, relevant types,
                                                      │  not quarantined, free units
                                                      ▼
                         prompt.build: system prompt (config) + compact context + JSON Schema (enums)
                                                      │
                                                      ▼
                         LlmBackend.generate  (OllamaBackend: /api/chat, streamed, hard deadline)
                                                      │
                                                      ▼
                         validate.check ── ok ──▶ brain/internal/proposal + brain/event llm_proposal
                                         └ refused ▶ brain/event llm_invalid_output
```

| File | Role |
|---|---|
| `service.py` | Message routing, queue, time budget, status, events. No MQTT (testable alone) |
| `context.py` | City state from internal topics; graph client; compact rendering under the char limit |
| `prompt.py` | System + user prompt; answer JSON Schema with per-incident enums |
| `validate.py` | Strict answer check: JSON, known target, allowed action, not quarantined, bounds |
| `backends.py` | `LlmBackend` protocol; `OllamaBackend` (streamed, structured output, health, warm-up) |
| `incident.py` | Tolerant reader of the graph team's incident message |
| `bench.py` | Benchmark on Test Book cases through the real service |
| `__main__.py` | MQTT wiring, Last Will, worker thread, health tick |
| `mocks/mock_llm/` | Scripted backend in four modes, run through the same service |

**Why these choices**

- *Ollama* over llama.cpp server: one install on Windows, model management (`ollama pull`), JSON-Schema structured output, and the runtime is behind `LlmBackend`, so llama.cpp can be added as another backend without touching the service.
- *Local context first*: the graph API does not exist yet; the retained internal topics already hold the city, quarantines and unit states, so the LLM never depends on another module being up.
- *Relevant device types per incident type*: measured, not theoretical. With area-only filtering, the 7B model followed the B10 injected text and proposed opening two valves on an accident; with the filter it cannot, because valves are neither shown nor allowed by the schema.
- *Action hints*: the city description lists actions without effects; without hints the model closed valves on floods (B2/B8 0 % correct); with them 100 %.
- *Streaming*: `urllib` timeouts are per socket operation, so a non-streamed call overran the 8 s budget (10.07 s measured); streaming makes the deadline hard and stops generation on the GPU.
- *127.0.0.1*: on Windows, `localhost` costs ~2 s per request (IPv6 tried first); measured 4.6 s → 2.5 s.

## 3. Interfaces

**Consumes**

| Topic / API | Fields used |
|---|---|
| `brain/internal/incident` | `incident_id`, `correlation_id`, `type`, `zone_id`, `status`, `severity`, `confidence`, `verified`, `evidence[]` (`source_id`, `kind`, `value`, `unit`, `category`, `trust`). Unknown fields ignored |
| `brain/internal/meta` (retained) | `zones[].zone_id/adjacent`, `actuators[].actuator_id/type/zone_id/controls/actions`, `units[].unit_id/type/home_zone` |
| `brain/internal/quarantine/{device_id}` (retained) | `device_id`, `state` |
| `brain/internal/ack` | unit `status`, `zone_id`; actuator `state` |
| `brain/internal/call` | `zone_id`, `category`, `text` |
| `GET {graph_url}/context/{incident_id}` (optional) | `quarantined[]` (ids or `{device_id}`), `related[]` / `related_events[]` (strings or objects with `type`, `kind`, `zone_id`, `status`, `summary`) |

**Publishes**

| Topic | Format |
|---|---|
| `brain/internal/proposal` | `contracts/schemas/internal-proposal.schema.json`: `id` (`llm-…`), `timestamp`, `correlation_id` (copied from the incident), `incident_id`, `origin: llm`, `actions[{target_id, action, params}]`, `justification` |
| `brain/internal/llm-status` (retained, Last Will) | `internal-llm-status.schema.json`: `status` `online`/`offline`, `model`, `reason` |
| `brain/event` (sealed like the input module's events) | `TimelineEvent`: `llm_proposal`, `llm_timeout`, `llm_invalid_output`, `llm_skipped`, `llm_online`, `llm_offline` |

## 4. What can be built alone and what needs other teams

| Item | Alone? | With |
|---|---|---|
| Service, validation, mock, benchmark, prompt tuning | yes (done) | — |
| `/context` JSON shape and port | no | Graph (person B): confirm or adapt `merge_graph`; `graph_url` in config |
| Incident message model in `common/models.py` | no | Graph (person A) owns it; this module reads it tolerantly meanwhile |
| Unit actions (`unit_actions`, now `dispatch`) and command format | no | Output A (BR-1 command body) |
| Action effects (`action_hints`), action names (`close_inbound`, `green_corridor`, …) | no | Twin team / mock Twin: actions come from `twin/meta` |
| Proposal format and how LLM and rules compete | no | Math (format already in `team-plan/00` §3) |
| Calls with free text and injections (B10, B11), "LLM offline" switch in the runner (B12) | no | Mock |

## 5. Running it at the demo

**Native (recommended).** Ollama and the LLM module run directly on the laptop, next to the GPU:
`ollama serve` (or the Ollama tray app, started at login), then `python -m src.llm` from `brain/`.

**In Docker.** If the Brain modules run in containers, Ollama still runs on the host (GPU
passthrough on Windows is fragile). Point `backend.url` at the host with a config override
(`BRAIN_CONFIG_DIR`): `http://host.docker.internal:11434`; on Linux add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service. To decide with the
person owning `docker-compose.yml` (no Dockerfile exists yet).

**Checklist (day before and on stage)**

- [ ] Model pulled and **pinned**: `backend.digest` matches `ollama list` (else `llm-status` is offline on purpose)
- [ ] Copy of `%USERPROFILE%\.ollama\models` (5.7 GB with the current model) on a USB drive, plus the Ollama installer
- [ ] `ollama ps` shows the model at **100 % GPU** (4.8 GB of the 8 GB VRAM); close games and GPU-heavy apps
- [ ] Laptop on mains power, power plan "best performance" (a GPU on battery is throttled)
- [ ] `backend.url` uses `127.0.0.1`
- [ ] Warm-up done at start (`llm_warmed_up` in `logs/llm.jsonl`), `llm-status online` retained on the broker
- [ ] Benchmark re-run on the laptop with the **whole stack running** (broker, timeline, all modules):
      `python -m src.llm.bench --cases tests/llm/cases_extended.yaml --repeat 3`
- [ ] Fallback drill: stop Ollama during a scenario → `llm-status offline` → rule fallback acts (B12)

## 6. Open points and limits

- **Throughput.** One generation at a time on the GPU (~2 s each): three simultaneous incidents fit in 8 s, a fourth times out and goes to the rule fallback. The queue serves `critical` and `high` incidents first, so under a burst the low-severity ones are the ones left to the rules.
- **Benchmark size.** 5 cases × 10 runs so far; the "≥ 95 % valid over 50 incidents" criterion needs a larger case set (ideally built with the mock team's scenarios).
- **Model choice.** `qwen2.5-coder:7b`, chosen after benchmarking three models (`docs/report/brain-llm.md`): 98 % correct on the 62-case set versus 83 % for `qwen2.5:3b` (2.4× faster, the fallback model) and 0 % for `llama3.2:3b`. On battery the 7B timed out in every run: keep the laptop on mains power.
- **Guardian trust is not re-weighted here.** The incident's `confidence`, `verified` and evidence `trust` are shown to the model, but trust is enforced by detection, scoring and the safety rules, not by the LLM.
- **Strict mode** refuses a whole answer for one bad action; switch `validation.strict: false` if the math team prefers partial proposals.
