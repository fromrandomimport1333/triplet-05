# City Brain: scoring and rule fallback (report paragraph)

**What.** The math module decides. For every incident it publishes a rule proposal at once, waits a short time for the local LLM's proposal, scores both and publishes the best one as a decision. Each decision lists the chosen actions, the score, the alternatives with their scores, the risk, the evidence sensors, the mode (normal or safe) and a one-line explanation, which also goes on the timeline. The rule fallback guarantees a decision even when the LLM is slow, offline or wrong.

**How.** The rule fallback is data (`config/rules.yaml`), not code. It holds one action template per target type for each incident type: for example, flood means opening the valves that control the zone and giving traffic priority in that zone. The templates are resolved against the city description, never using a quarantined device, a busy unit or a target that already failed. The module then waits for the LLM proposal for up to the LLM budget (8 s). It decides at once when the LLM reports itself offline or gives up on the incident, and also when severity is critical, in which case it switches later if a better LLM proposal arrives. Every proposal is checked first: unknown devices, actions that are not allowed, quarantined devices and failed targets are dropped. Then units are allocated and the proposal is scored. All parameters are in `config/scoring.yaml`:

```
P            = confidence x severity_weight x zone_importance x (1 + waiting_s / 60)
action score = P x effectiveness - cost - risk x (1 - trust)  [- 2.5 x risk x P in safe mode]
proposal     = sum of the action scores > 0   (actions scoring <= 0 are dropped and noted)
```

Severity weights are 1 to 4. Zone importance is 2 for the hospital zone, 1.5 for high-exposure zones and 1 otherwise. Effectiveness is a table per (incident type, target type, action). The cost is `0.3 x hops / 4` plus 0.2 per unit used (0.05 per actuator), where hops are zone hops found by a breadth-first search on the adjacency of the city description. The risk is set per action: grid shed 0.7, valve open 0.6, unit dispatch 0.4, signal 0.1. The trust is the mean trust of the incident's evidence, and Guardian's latest verdict for a sensor overrides the trust detection had given it. The engine keeps the score of every proposal it evaluates and logs it, along with the full breakdown, for tuning.

*Worked example* (computed by the code: `python -m tests.scoring.worked_example`). A flood at zone-01 (port, high exposure), severity medium, confidence 0.8. The evidence is water-01 (trust 0.9) and a flood call (trust 1.0), and the LLM answers after 3 s. P = 0.8 x 2 x 1.5 x 1.05 = 2.52 and the trust is 0.95.

| Proposal | Action | eff | hops | cost | risk x (1 - trust) | score |
|---|---|---|---|---|---|---|
| rules | open valve-01 | 0.9 | 0 | 0.050 | 0.6 x 0.05 = 0.030 | 2.188 |
| rules | priority signal-01 | 0.3 | 0 | 0.050 | 0.005 | 0.701 |
| | **total** | | | | | **2.889** |
| llm | open valve-01 | 0.9 | 0 | 0.050 | 0.030 | 2.188 |
| llm | priority signal-02 | 0.3 | 1 | 0.125 | 0.005 | 0.626 |
| llm | dispatch unit-rescue-01 | 0.6 | 1 | 0.275 | 0.4 x 0.05 = 0.020 | 1.217 |
| | **total** | | | | | **4.031** |

The timeline shows: *"llm proposal (score 4.03) chosen over rules (2.89): open valve-01, priority signal-02, dispatch unit-rescue-01 to zone-01"*.

**Why.** The score fits on one slide and every term has a meaning the jury can check. Guardian's trust counts twice: through the confidence of detection, and in the risk penalty, so risky actions based on doubtful data cost more. In safe mode (Guardian offline, or an unverified incident) the penalty grows with P. A risky action then keeps a positive score only if its effectiveness is above 2.5 x its risk, so traffic signals win unless two different sensor types agree (B4). If all evidence has trust 0, the module makes no decision and says why on the timeline (B3, defence in depth behind detection). Units go to the higher-priority incident. A free unit that an undecided incident of higher priority also wants is left for it, and the numbers appear in the explanation (B5). A unit already held by another incident is only taken if the new priority is at least 1.5 times higher. Because the Twin refuses to dispatch a busy unit, taking it means a recall now and a dispatch when the Twin reports the unit free. After a failed or refused action, the module decides again without that target. Later decisions contain only new actions, so nothing is sent twice.

**Limits.** The effectiveness and risk tables are hand-set estimates, tuned on the mock scenarios, not learned from data. Summing action scores favours proposals with more actions, which is limited only by the cost of each action and by dropping actions that score 0 or less. The rule fallback has no "upstream valve" knowledge, because the city description does not give water flow. A decision is never undone: when the module switches to a better proposal, actions already sent stay in effect, and closing an incident does not recall its units. The waiting time for the LLM is a fixed budget, so a correct but slow LLM answer is lost.
