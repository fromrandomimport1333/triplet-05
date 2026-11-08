# Team 4: Math Model (scoring + rule fallback)

**People:** 1 · **Code:** `brain/src/scoring/`, `brain/src/rules/` · **Read first:** [`00-overview-and-contracts.md`](00-overview-and-contracts.md)

## 1. Mission

You **decide**. For each incident you receive proposals (from the LLM and from your own rule fallback), give each a **score**, and pick the best one. The score uses **Guardian's trust**, so actions based on doubtful data score lower. You also own the **rule fallback**, which guarantees a proposal for every incident even when the LLM is slow or offline.

## 2. You own / you don't own

| You own | You don't own |
|---|---|
| The scoring model (formula, parameters, explanation) | Detecting incidents (Graph) |
| Choosing the best proposal | LLM proposals (LLM) |
| Rule fallback: standard action per incident type | Safety rules: the hard checks after you (Output A) |
| Resource choice: which unit / actuator when several are possible | Sending commands (Output A) |

## 3. Your inputs and outputs

| Direction | Topic / API | Content |
|---|---|---|
| In | `brain/internal/incident` | Incidents (trigger for the rule fallback) |
| In | `brain/internal/proposal` | Proposals from the LLM (`origin: llm`) and your rules (`origin: rules`) |
| In | `brain/internal/trust/#`, `quarantine/#`, `layer-status/#` | Guardian trust, quarantined devices, Guardian online/offline |
| In | Graph API `/zone`, `/actuators` | Neighbours, which actuators control the zone, unit positions |
| In | `brain/internal/outcome` | Whether actions worked (for re-deciding) |
| Out | `brain/internal/proposal` with `origin: rules` | Rule fallback proposals |
| Out | `brain/internal/decision` | The chosen actions with scores and alternatives |
| Out | `brain/event` | `decision` event with the score and why |

## 4. Rule fallback

On every **opened** or **updated** incident, publish a rule proposal immediately:

| Incident | Standard action |
|---|---|
| `flood` | Open the valve controlling the zone (upstream if known) · close inbound traffic signals |
| `fire` | Dispatch the nearest free fire unit · clear the route (traffic signals) |
| `accident` | Dispatch the nearest free ambulance · divert traffic |
| `traffic_jam` | Retime the zone's traffic signals |
| `power_problem` | Switch the grid to the backup line |

Rules live in `config/rules.yaml`, not in code.

## 5. When to decide

- Wait for the LLM proposal up to the **LLM time budget** (8 s, from config).
- If the LLM answers in time: score both, pick the best.
- If not (timeout, `llm-status = offline`, invalid answer): decide on the rule proposal alone.
- Severity `critical`: decide on the rule proposal **immediately**, then switch if a better LLM proposal arrives.

## 6. The scoring model (starting proposal, yours to own and improve)

**Goal:** a score that is **simple to explain on one slide**, uses **Guardian's trust**, and prefers **effective, cheap, low-risk** actions.

### 6.1 Priority of the incident
```
P = confidence × severity_weight × zone_importance × (1 + waiting_time / 60 s)
```
| Term | Values |
|---|---|
| `confidence` | From the incident (0–1). Already includes Guardian's trust weights from detection |
| `severity_weight` | low 1 · medium 2 · high 3 · critical 4 |
| `zone_importance` | normal 1 · high (port, school) 1.5 · critical (hospital) 2, from the city description |
| waiting time | Seconds since the incident opened, so waiting incidents gain priority |

### 6.2 Score of a proposal
```
Score = P × effectiveness − cost − risk_penalty
```
| Term | Meaning |
|---|---|
| `effectiveness` | 0–1: how well this action usually solves this incident type (table in `config/scoring.yaml`) |
| `cost` | Travel distance (zone hops from the graph) and units used, normalised 0–1 |
| `risk_penalty` | Higher for risky actions (valve, grid, dispatch) when the evidence is weak: `risk × (1 − trust of the evidence)` |

**Guardian's trust** enters twice: in `confidence` (through detection) and in `risk_penalty` (risky actions on doubtful data cost more). If Guardian provides a `trust_score`, use it directly.

**Guardian offline (safe mode):** add a large penalty to risky actions, so low-risk actions (traffic signals) win unless several different sensors agree.

### 6.3 Choosing units and actuators
When several units or actuators can do the job: pick the one with the best score, never a **quarantined** device, never a **busy** unit unless the new incident's priority is much higher (e.g. 1.5×).

### 6.4 Explaining the decision
Every `decision` message lists the chosen proposal, its score, the alternatives and their scores. The timeline shows: *"valve-port-up (score 1.53) chosen over rules proposal (1.21)"*.

## 7. Tasks

### Week 1 (15–19 Oct)
- [ ] `config/rules.yaml` and the rule fallback publishing proposals for fake incidents
- [ ] First version of the score in a spreadsheet: check it ranks hand-made cases sensibly
- [ ] **Fri 17 Oct:** proposal and decision formats frozen with LLM and Output teams

### Week 2 (20–26 Oct)
- [ ] Scoring service: wait for LLM / timeout, score, publish decision
- [ ] Trust, quarantine and safe mode taken into account
- [ ] **Sun 26 Oct:** end-to-end run on mocks

### Week 3 (27 Oct – 2 Nov)
- [ ] Unit / actuator choice with the graph (distance, busy units)
- [ ] Re-decide when an action failed (outcome `failed`): next best proposal
- [ ] Tune parameters on all scenarios; joint run

### Week 4 (3–7 Nov)
- [ ] Report: the model in plain words + formulas, a worked example with numbers, limits

## 8. Done when

- [ ] Every opened incident gets a decision, **even with the LLM offline** (B12)
- [ ] **Fake flood:** no decision (or score ≤ 0) because the evidence is rejected (B3)
- [ ] **Fire + accident, one ambulance:** the higher-priority incident gets it, numbers visible (B5)
- [ ] **Guardian offline:** risky actions not chosen on a single sensor (B4)
- [ ] Never selects a quarantined device (B8)
- [ ] A worked example for the report, computed by your code

## 9. Who you work with

| Team | About |
|---|---|
| **Graph** | Incident fields; distance and actuator queries |
| **LLM** | Proposal format; time budget |
| **Output A** | Decision format; safety rules that apply after you; outcomes |
| **Mock** | Scenarios with competing incidents and limited units |

## 10. Risks and tips

- Keep the formula **simple**. A score you can't explain in 30 seconds to the jury is a weakness, not a strength.
- All parameters in `config/scoring.yaml`; log the score details of every decision for tuning.
