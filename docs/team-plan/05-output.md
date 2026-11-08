# Team 5: Output (safety + commands + timeline)

**People:** 2 · **Code:** `brain/src/output/` (person A), `brain/src/timeline/` (person B) · **Read first:** [`00-overview-and-contracts.md`](00-overview-and-contracts.md), [`../city-brain-interface-spec.pdf`](../city-brain-interface-spec.pdf) sections 4, 8, 9

## 1. Mission

You are the **last gate and the voice of the City Brain**.

| Person | Role |
|---|---|
| **A: Safety + commands** | Applies the **safety rules** no score can override, signs and sends commands to the Twin, checks they were acknowledged |
| **B: Timeline + logs** | Makes everything the Brain does **visible**: live timeline screen, log files per scenario (Phase 1 requirements R2 and R3) |

## 2. You own / you don't own

| You own | You don't own |
|---|---|
| **A:** safety rules, ② signing, publishing `brain/cmd/…`, tracking acknowledgements, retries, alerts | Choosing the action (Math) |
| **B:** `brain/event` collection, timeline screen, log files, `/chain` display | Checking incoming messages (Input) |

## 3. Your inputs and outputs

| Direction | Topic | Content |
|---|---|---|
| In (A) | `brain/internal/decision` | Chosen actions to execute |
| In (A) | `brain/internal/quarantine/#`, `trust/#`, `layer-status/#` | For the safety rules |
| In (A) | `brain/internal/ack` | Twin acknowledgements |
| Out (A) | `brain/cmd/{actuator_or_unit_id}` | **Signed** commands (external, spec BR-1) |
| Out (A) | `brain/internal/outcome` | sent / refused / acknowledged / failed |
| Out (A) | `brain/alert` (optional, if agreed with Guardian) | e.g. actuator not responding |
| In (B) | `brain/event`, `brain/error` | Everything to show |
| In (B) | Graph `GET /chain/{correlation_id}` | Full cause → effect chain |
| Out (B) | Timeline screen, `logs/<scenario>-<time>.log` | What the jury sees |

## 4. Person A: safety rules

Applied to **every action** of a decision, in this order. If one fails, the action is **refused** (never silently): publish `outcome: refused` with the rule, and a `brain/event`.

| # | Rule | Refused when | Example |
|---|---|---|---|
| S1 | Quarantine | Target device is quarantined | `valve-port-main` quarantined → refused |
| S2 | Rejected evidence | All evidence of the incident comes from rejected readings | Fake flood → refused |
| S3 | Two sensors for risky actions | Valve, grid or dispatch with fewer than **2 different sensors** agreeing | One water sensor only → refused (waits) |
| S4 | Cooldown | Same action on the same device within **60 s** | Signal switched again after 10 s → refused |
| S5 | Allowed action | Action not in the device's allowed actions (city description) | `open` on a traffic signal → refused |
| S6 | Guardian offline | Risky action while Guardian is offline, unless S3 is met with **2 different sensor types** | — |

Rules and values in `config/safety.yaml`. Each rule has its own unit test.

## 5. Person A: commands and acknowledgements

1. Build the command body (spec BR-1) with a new `id` (e.g. `brain-000042`) and the `correlation_id`.
2. **Sign:** envelope `{"v":1,"body":"<text>","hmac":"<HMAC-SHA256 with the Brain–Twin key>"}`.
3. Publish on `brain/cmd/{target_id}`, QoS 1. Outcome `sent`.
4. Wait for the acknowledgement (`brain/internal/ack` with `correlation_id` = command id):
   - `done` within **2 s** (configurable; real hardware may need more) → outcome `acknowledged`
   - `rejected` / `failed` → outcome `failed` with the reason
   - nothing after 2 s → **retry once with the same id**, then outcome `failed` + `brain/alert` `actuator_not_responding`
5. Optional (week 3): check the **effect** (valve opened → water stops rising within 30 s); if not, outcome `failed` so Math picks another action.

## 6. Person B: timeline and logs

### 6.1 Timeline screen
A web page (e.g. FastAPI + WebSocket, or a simple JS page subscribing to MQTT over WebSocket) showing a live list:

```
10:22:03.120  TWIN      water-level-04 = 2.4 m (port)
10:22:03.910  GUARDIAN  water-level-04: trusted
10:22:04.200  BRAIN     incident opened: flood at port (confidence 0.76)
10:22:05.900  BRAIN     LLM proposal: open valve-port-up 80%
10:22:05.950  BRAIN     decision: valve-port-up (score 1.53) over rules (1.21)
10:22:06.010  BRAIN     command sent → valve-port-up
10:22:06.400  TWIN      ack: valve-port-up open_80
```
- Colour per layer; refused actions and errors in red.
- Click an action → show its full chain (`/chain/{correlation_id}`).
- **Coordinate with the Twin and Guardian teams:** the timeline must show all three layers (R3). The Guardian team proposes `triplet/timeline/{layer}` topics; agree one format with them through the team lead.

### 6.2 Logs (R2)
- Every scenario run writes **one log file**: `logs/<scenario-id>-<YYYYMMDD-HHMMSS>.log`, JSON lines, all `brain/event` and `brain/error` messages.
- Must be readable by the jury after the event.

### 6.3 Events you must make sure exist
`incident_opened/updated/closed`, `llm_proposal`, `llm_timeout`, `decision`, `action_refused`, `command_sent`, `command_acknowledged`, `command_failed`, `guardian_offline`, `safe_mode_on/off`, `input_rejected`. Check with every team that they publish theirs.

## 7. Tasks

### Week 1 (15–19 Oct)
- [ ] **A:** signer (envelope + HMAC), verified with the spec's test vector; publish a test command to the mock actuators
- [ ] **B:** event format frozen with all teams (**Fri 17 Oct**); first timeline page listing `brain/event`
- [ ] **Both:** `config/safety.yaml`, outcome format

### Week 2 (20–26 Oct)
- [ ] **A:** safety rules S1–S6, acknowledgement tracking, retry, outcomes
- [ ] **B:** log file per scenario run; colours; refused / error display
- [ ] **Sun 26 Oct:** end-to-end run on mocks, visible on the timeline

### Week 3 (27 Oct – 2 Nov)
- [ ] **A:** effect check (optional), `brain/alert`
- [ ] **B:** chain view; integrate Twin and Guardian events into the shared timeline
- [ ] Joint run: timeline on the big screen

### Week 4 (3–7 Nov)
- [ ] Report: safety rules, signing, acknowledgements (A); timeline and logs (B)
- [ ] **B:** the timeline is the demo screen; prepare its layout for the projector

## 8. Done when

- [ ] Each safety rule refuses its case and lets the valid case through (unit tests)
- [ ] Scenarios B3 (fake flood), B4 (Guardian offline), B8 (quarantined valve): the right action refused, with the reason on the timeline
- [ ] Commands accepted by the mock actuators (valid HMAC); duplicate not executed
- [ ] Missing ack → retry → `command_failed` + alert
- [ ] Every scenario run produces exactly one log file
- [ ] The timeline shows a full flood chain from reading to ack in real time

## 9. Who you work with

| Team | About |
|---|---|
| **Math** | Decision format; refused / failed outcomes so they can re-decide |
| **Input** | Shared MQTT client; acks, quarantine, layer status |
| **Graph** | `/chain` endpoint |
| **All teams** | The events each must publish |
| **Team lead** | Shared timeline format with the Twin and Guardian teams |

## 10. Risks and tips

- The timeline is what the jury **looks at for 8 minutes**: make it readable from 5 metres.
- Never block a command forever waiting for an ack: always time out.
- Test the signer against the Twin team's verifier **early**, not at the joint run.
