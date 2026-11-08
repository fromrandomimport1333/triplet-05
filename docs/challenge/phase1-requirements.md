# Phase 1 requirements (summary of the organizers' Phase 1 Specifications)

Source: *City Brain Convergence — Phase 1 Specifications* (TSYP14, 4 pages). The PDF itself is
not in the repository. Section numbers below (§1–§12) are those of the PDF. Wording is
summarised, not reinterpreted; where something is an inference, it is marked as such.

## The challenge (§1, §2)

- Each **triplet** (one Twin team, one City Brain team, one Guardian team) builds one working
  smart-city system. **There is no imposed technical contract**: architecture, languages,
  protocols (REST, MQTT, WebSocket, gRPC, MCP…), hosting, databases, AI/ML methods, UI and test
  scenarios are free (§3).
- Period: 24 Sep – 14 Nov 2026 (CSTAM). Same triplet until CSTAM.

| Layer | Role | Key question |
|---|---|---|
| Twin | Simulates the city: zones, sensors (traffic, water level, power load, air quality, emergency calls…) and actuators (dispatch, traffic signals, valves, grid switches…) | What is happening in the city? |
| **City Brain** | Detects real incidents (accidents, fires, jams, floods) from the live data, decides the right response, and acts through the Twin's actuators | **What should we do?** |
| Guardian | Watches the same data for sensor faults, actuator failures, spoofed data and cyber-attacks; gives trust verdicts; contains problems (isolate, quarantine, roll back) | Is this data telling the truth? |

## The four required rules (§3)

| # | Requirement | Why (organizers) |
|---|---|---|
| R1 | Every layer runs on its own, with simple **mocks of the two other layers**. Each team writes a one-page **Interface Card** (`INTERFACE.md`): what the layer sends and receives, one example of each message, how to point it at a different partner (**configuration, never hard-coded**), and how it authenticates. | Shows the layer works independently and is designed to connect to others |
| R2 | Every scenario starts with **one trigger** (one command or button) and each run produces a **log file stored in the repository**. | Short demos; the jury checks reliability after the event |
| R3 | A **live timeline screen** shows what happens across the three layers with timestamps (e.g. reading → Guardian verdict → Brain decision → actuator action). | Judges must see cause and effect |
| R4 | The triplet documents how the three teams coordinated, how they handled a partner failure or change, and proof of a successful joint run. | Shows teamwork, not three independent layers |

## Interoperability (§4)

Phase 2 triplets are reshuffled, so interoperability is part of the Phase 1 score. The jury looks for:
- a clear separation between the layer's logic and the way it talks to partners;
- a complete Interface Card with documented message formats and examples;
- partner addresses and settings in configuration files, not hard-coded;
- standard, well-known protocols and data formats;
- input validation and clear error messages when a partner sends something unexpected;
- a layer that runs on its own with mocks (R1).

## Dates and deliverables (§5, §6)

| Date | What |
|---|---|
| **7 Nov 2026** | Phase 1 submission: repository, report and pitch deck |
| 9–13 Nov 2026 | Jury review of repositories, reports and logs |
| **14 Nov 2026** | CSTAM: 2-minute pitch + 8-minute live demo per triplet |

| Deliverable | Content |
|---|---|
| GitHub repository | Named `triplet-XX`; structure of §7, including scenario scripts and run logs |
| Report (PDF, ≤ 20 pages) | Five parts (below), includes the Test Book |
| Pitch deck (PDF) | ~5 slides: problem, solution, architecture, what makes it reliable and trustworthy |

## Repository structure (§7)

One repository per triplet, one folder per team (`twin/`, `brain/`, `guardian/`), each with
`README.md` (run alone), `INTERFACE.md`, `src/`, `mocks/`, `tests/`. Root: `README.md`,
`INTEGRATION.md`, `TESTBOOK.md`, `docker-compose.yml` (whole city with one command,
recommended), `.github/CODEOWNERS` (each team commits only in its folder), `scenarios/` (one
trigger script per Test Book scenario), `logs/` (one log file per run), `docs/` (`report.pdf`,
`pitch.pdf`, `diagrams/`).

## Report structure (§8, ≤ 20 pages)

| Part | Content | Pages |
|---|---|---|
| 0. Triplet overview & integration | Global architecture diagram, how layers communicate, one scenario traced end to end, shared decisions | 3 |
| 1. The City (Twin) | City plan and zones, sensors and actuators, simulation model, how scenarios are played, tech stack | 5 |
| **2. The City Brain** | **Incident detection, decision method (rules, ML or AI agent), how it picks an actuator, tech stack** | **5** |
| 3. The Guardian | Threats covered, detection methods, containment actions, false alarms, tech stack | 5 |
| 4. Test Book | Scenarios with expected outputs | 1 |
| 5. Collaboration process | Tools/workflow, how a partner failure or change was handled, evidence of a joint run | 1 |

Parts 1–3 use the same six subheadings: **Role and goals · Architecture · Tech stack and why ·
Interfaces in/out · Security measures · Known limitations.**

## Test Book (§9)

No mandatory list; taken together, scenarios must test all three layers and every interaction:

| Interaction | What it shows | Example ideas (inspiration only) |
|---|---|---|
| Twin → Brain | The city runs normally and the Brain reacts correctly to real events | A calm day where the Brain must not overreact; a rush-hour jam; a flood that needs a valve and a unit |
| Twin → Guardian | The Guardian recognises healthy and unhealthy data | A sensor gets stuck or dies; a fake reading; a device floods the network |
| Brain ↔ Guardian | Trust verdicts change decisions, and decisions stay safe | The Brain ignores a fake incident; the Guardian quarantines a device the Brain wants to use |
| All three | The whole city works as one system, including when something goes wrong | A real incident and an attack at the same time; one layer goes down and the city keeps running |

Fields per scenario: ID, Name, Layers involved, Description, Trigger, Expected (per layer), How
to verify. Organizers' example **T5 "Fake flood at the port"**: the port sensor reports 180 cm
while the real level is 40 cm; Guardian marks it untrusted **within 10 s** and isolates the
sensor; the Brain **does not open the valve, and records why**; visible on the timeline and in
`logs/t5-*.log`.

## CSTAM demo (§10)

- System already running and connected before the slot; readiness check = timeline shows live data.
- 0:00–2:00 pitch (strict); 2:00–10:00 live demo of chosen Test Book scenarios; others are checked through the logs. Judges may ask questions.
- If the live connection fails: **2 minutes to recover**, then the jury relies on code and logs and live-demo points are reduced.
- **Do not rely on the venue Wi-Fi** (bring a router or hotspot). Cloud use needs a **local fallback**. Nothing is installed on stage.

## Scoring (§11), per team, out of 50

| Criterion | Points |
|---|---|
| Individual Prototype Functionality | 10 |
| Technical Implementation & Features | 5 |
| **Interoperability, Connectivity & Collaboration Process Between the 3 Teams** | **15** |
| **Security & Trust Mechanisms** | **10** |
| Documentation & Code Quality | 5 |
| Demonstration & Presence | 5 |

Each team gets its own score. *"A team whose layer behaves correctly is not penalized for a
partner's failure, provided it shows this with its mocks."*

## After Phase 1 (§12)

N teams are selected on their Phase 1 score, balanced across layers, and reshuffled into new
triplets right after CSTAM; Phase 2 details are shared later. Contact: yp@ieee.tn, cs@ieee.tn, cn@ieee.tn.

*Inference (not stated by the organizers):* 25 of the 50 points (interoperability + security)
depend on how a layer talks to partners and treats untrusted input, versus 10 for raw
functionality; this is why the team plan keeps every partner-facing setting in configuration
and every message validated.
