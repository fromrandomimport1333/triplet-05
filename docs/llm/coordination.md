# LLM module: points to agree with the other teams

The LLM module works today with the assumptions below. Each needs a short confirmation from
the team that owns the other side. When a point is settled, update the file named in the last
column and tick it here.

## Graph (detection + graph DB)

- [ ] **`GET /context/{incident_id}`: shape and port.** Assumed: JSON object with optional
  `quarantined` (list of ids or `{device_id}`) and `related` / `related_events` (strings, or
  objects with `type`, `kind`, `zone_id`, `status`, `summary`); anything else is ignored. Port
  assumed `8080`. *Needed:* the real shape and address, answering in < 1 s (budget
  `context_s`). → `config/llm.yaml` `context.graph_url`, `src/llm/context.py` `merge_graph`.
- [ ] **Incident model in `src/common/models.py`.** The incident format of `team-plan/00` §3
  has no pydantic model or JSON Schema yet. The LLM reads it tolerantly
  (`src/llm/incident.py`); the owning team should add the model so the schema exists in
  `contracts/schemas/`. *Needed:* model + schema, and confirmation that `severity` uses
  `low/medium/high/critical` (the LLM queue orders by it).
- [ ] **Updates.** The LLM is called on every `opened` and `updated` message. *Please confirm*
  updates are only sent on new evidence or a severity change (`team-plan/02` §4.4), not on
  every reading, otherwise the GPU queue fills up.

## Output (safety + commands)

- [ ] **What units accept.** `twin/meta` lists no actions for units; the LLM assumes
  `dispatch` for every unit type. *Needed:* the unit actions of the command format (BR-1).
  → `config/llm.yaml` `unit_actions`.
- [ ] **`params`.** Proposals may carry `params` (e.g. `{"percent": 80}` for a valve). *Needed:*
  which params the command format accepts, so validation can bound them.
- [ ] **Timeline.** LLM events: `llm_proposal`, `llm_timeout`, `llm_invalid_output`,
  `llm_skipped`, `llm_online`, `llm_offline` on `brain/event`, sealed like the input module's
  events. *Please confirm* the timeline shows them, and whether the justification should
  appear in the `llm_proposal` summary.

## Math (scoring + rule fallback)

- [ ] **Strict or partial answers.** In `strict` mode an answer with one unknown device or
  not-allowed action is refused entirely (`llm_invalid_output`); with `strict: false` the bad
  action is dropped and the rest proposed. *Needed:* your preference. → `config/llm.yaml`
  `validation.strict`.
- [ ] **When to stop waiting.** The LLM publishes `llm_timeout` (with `incident_id`) as soon as
  it gives up, and immediately while `llm-status` is `offline`. *Please confirm* you use these
  to decide on the rule proposal early instead of always waiting the full 8 s.

## Twin team (through the team lead) and Mock

- [ ] **Action names and effects.** The LLM only knows what an action does from
  `action_hints` (e.g. valve `open` = drains water from the zone). Measured: without the hints
  the model closed valves on floods (0 % correct), with them 100 %. *Needed:* the real action
  names in `twin/meta` and what each does in the simulation. → `config/llm.yaml`
  `action_hints`, `relevant_targets`.
- [ ] **Device types.** `relevant_targets` uses the types `valve`, `traffic_signal`,
  `grid_switch`, `ambulance`, `fire`, `police`, `rescue`. *Needed:* the type names the Twin
  uses.
- [ ] **Calls with free text (Mock).** B10 (injection) and B11 (compound) need calls with real
  text, including French / Tunisian Arabic and injection attempts; examples are in
  `tests/llm/make_cases.py` (`CALLS`, `INJECTIONS`). Call ids must be unique per run.
- [ ] **LLM switch in the scenario runner (Mock).** B12 needs the runner to stop the LLM or to
  start `python -m mocks.mock_llm offline`; scenario files can choose a mock mode
  (`scripted`, `slow`, `offline`, `invalid`).

## Team lead / Docker

- [ ] **Where the LLM runs at the demo.** Natively on the laptop (recommended), or in a
  container reaching Ollama on the host (`host.docker.internal:11434`). See
  `requirements-and-design.md` §5.
- [ ] **Broker ACL.** Only Brain accounts may publish on `brain/internal/proposal` and
  `brain/internal/llm-status`; a forged proposal would otherwise reach scoring.
