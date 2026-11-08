# City Brain: output module (report paragraph)

**What.** The output module is the last gate of the City Brain and the only part that commands the city. It receives the actions chosen by the scoring model, applies six safety rules that no score can override, and sends what passes to the Twin as signed commands. It then follows each command until the Twin acknowledges it and the readings show that the city actually reacted. Every refusal is published with its rule and its reason, on the internal outcome topic and on the timeline. A refusal is never silent.

**How.** Each action goes through the rules in a fixed order, stopping at the first failure:
- S1: the target is quarantined by Guardian.
- S2: all the evidence of the incident was rejected.
- S3: a risky action (opening a valve, shedding grid load, dispatching a unit) has fewer than two different sources agreeing.
- S4: the same action on the same device was sent less than 60 s ago.
- S5: the action is not allowed for that device in the city description, or the target is unknown.
- S6: Guardian is offline and a risky action does not have two different sensor types behind it.

Evidence comes from the incident, with Guardian's latest verdict applied. Each sensor counts as one source, and all the emergency calls of an incident count together as one source. A command gets a new id (`brain-000042`), keeps the chain's `correlation_id`, and is sealed with one HMAC for the Twin and one for Guardian, then published with QoS 1. No acknowledgement within 4 s means one retry of the exact same signed message. A second silence marks the command `failed` and raises an `actuator_not_responding` alert. After a `done`, an effect check watches the zones the device controls for 30 s: a valve must make the water level turn down. If nothing moves, the command is marked `failed` with `no_effect`, so the scoring model picks another action. Nothing ever waits: timeouts are checked by a periodic tick.

**Why.** The LLM and the scores can be wrong, and an attacker can fake sensors or calls. The rules bound the damage of any upstream error: a fake flood from rejected readings (B3), a quarantined valve (B8), or ten fake calls from one zone (B7) cannot move a device. Calls count as one source because they are easy to fake in bulk and cannot be checked by Guardian. Two sensor types are required when Guardian is offline, because without its verdicts a single compromised sensor family could otherwise trigger an action. Retrying with the same id makes the retry safe: the Twin never applies a command twice. The effect check closes the loop. An acknowledgement only proves that the message arrived, not that the valve moved (B6).

**Limits.** The effect check knows only the expected direction per action type: water down, load down, traffic speed up, particulates down. It cannot tell a working valve from a flood that recedes on its own at the same moment. A valve that only stops the rise without lowering the level is reported as `no_effect`, which makes the Brain cautious rather than wrong. The rules trust the city description for allowed actions and the incident for its evidence, so a wrong description or a detection bug passes through them. Command ids are unique within a day. A restart on another day could reuse an id that the Twin still remembers.
