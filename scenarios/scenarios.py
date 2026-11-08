"""The Test Book scenarios: what is triggered, what each layer must do, and how a run is judged.

run_scenario.py runs these and TESTBOOK.md is generated from them, so the two can never disagree.
To add a scenario: write a function run(ctx) and add an entry to SCENARIOS.

T1-T8 are the Phase 0 test plan (report Table 10, as listed in PROMPT_twin_only.md Phase E) plus the tampered-message
check; T9-T12 are extra fault kinds. Sensor ids belong to the demo city in twin/city.yaml: WATER_07 / WATER_08 are the
upstream / downstream canal gauges served by VALVE_03 in zone_a, which also holds TL_01, ROAD_01 and ALERT_01.

Checks marked only="mocks" test a behaviour of the Twin team's stand-ins that the real layers may implement differently
(they are reported as SKIP with --layers real).
"""
import json
import time

WATER, WATER_UP, VALVE, ALERT, ROAD, SIGNAL, DISPATCH = "WATER_08", "WATER_07", "VALVE_03", "ALERT_01", "ROAD_01", "TL_01", "DISPATCH_01"
JAM_ZONE = "zone_a"


# ---------------------------------------------------------------- helpers
def untrusted_event(ctx, since, sid):
    return ctx.find(since, "guardian/verdicts/", sensor_id=sid, status="untrusted")


def commands(ctx, since, actuator):
    return ctx.find(since, "brain/decisions", event="command_sent", actuator=actuator)


def feedback(ctx, since, actuator, **match):
    return ctx.find(since, "city/", type="feedback", actuator_id=actuator, **match)


def first_reason(found):
    return found[0]["msg"].get("reasons", [""])[0] if found else ""


def trusted_again(ctx, sid, timeout=45):
    """Trust comes back once the fault ends (a quarantined sensor needs a few clean windows first)."""
    return ctx.wait_for("%s trusted again" % sid, lambda: (ctx.verdict(sid) or {}).get("status") == "trusted", timeout)


def last_reading(ctx, sid):
    with ctx.lock:
        events = list(ctx.events)
    for e in reversed(events):
        if e["msg"].get("type") == "reading" and e["msg"].get("sensor_id") == sid:
            return e
    return None


def mark_when_true_level_above(ctx, sid, threshold):
    """Predicate helper: records the 'incident' mark the first time the TRUE level (ground truth) crosses the
    threshold the Brain acts on. Used for the KPI 'time to action'."""
    def check():
        if "incident" not in ctx.marks and (ctx.truth(sid) or 0) >= threshold:
            ctx.mark("incident")
        return False
    return check


def wait_flood(ctx, since, timeout=180):
    thr = ctx.cfg.get("flood_threshold_m") or 1.0
    watch = mark_when_true_level_above(ctx, WATER, thr)

    def pred():
        watch()
        return ctx.find(since, "brain/decisions", event="flood_detected")
    return ctx.wait_for("Brain detects the flood", pred, timeout)


# ---------------------------------------------------------------- T1 calm day
def t1(ctx):
    since = time.time()
    ctx.step("watching 30 s of calm running")
    time.sleep(30)
    bad = ctx.find(since, "guardian/verdicts/", status="untrusted")
    ctx.check("Guardian raised no alarm (no false positive)", not bad,
              "%d untrusted verdicts%s" % (len(bad), ": " + ", ".join(sorted({b["msg"]["sensor_id"] for b in bad})) if bad else ""))
    ctx.check("Brain took no decision", not ctx.find(since, "brain/decisions"))
    ctx.check("readings kept arriving", len(ctx.find(since, "city/", type="reading")) > 30)
    ctx.check("valve stayed closed", ctx.actuator_state(VALVE) in ("closed", "idle"), str(ctx.actuator_state(VALVE)))


# ---------------------------------------------------------------- T2 real flood
def t2(ctx):
    since = time.time()
    ctx.rain(60)
    flood = wait_flood(ctx, since)
    ctx.check("Brain detected the flood", flood, "level %s m" % (flood[0]["msg"].get("level_m") if flood else "?"))
    if not flood:
        return
    ctx.check("Guardian never marked the real flood as untrusted (no false alarm)",
              not untrusted_event(ctx, since, WATER) and not ctx.find(since, "brain/decisions", event="action_blocked"))
    sent = ctx.wait_for("Brain opens the valve", lambda: commands(ctx, since, VALVE), 25)
    ctx.check("Brain sent 'open' to the valve, with a verdict_ref", sent and sent[0]["msg"]["args"] == {"state": "open"}
              and sent[0]["msg"].get("verdict_ref"), sent[0]["msg"].get("verdict_ref") if sent else "")
    res = ctx.wait_for("valve feedback", lambda: feedback(ctx, since, VALVE), 15)
    ctx.check("valve confirmed it is open", res and res[0]["msg"]["ok"] and res[0]["msg"]["state"] == "open",
              res[0]["msg"]["state"] if res else "no feedback")
    ctx.check("public alert was sent", ctx.wait_for("alert", lambda: commands(ctx, since, ALERT), 15))
    ctx.check("Twin shows the valve open", ctx.actuator_state(VALVE) == "open", str(ctx.actuator_state(VALVE)))
    ctx.rain(0)
    cleared = ctx.wait_for("flood ends", lambda: ctx.find(since, "brain/decisions", event="flood_cleared"), 240)
    ctx.check("Brain declared the flood over", cleared)
    closed = ctx.wait_for("valve closed", lambda: ctx.actuator_state(VALVE) == "closed", 25)
    ctx.check("valve was closed again", closed, str(ctx.actuator_state(VALVE)))


# ---------------------------------------------------------------- T3 spoofed flood
def t3(ctx):
    since = time.time()
    truth = ctx.truth(WATER)
    ctx.line("SCENARIO", "true water level at %s: %s m (the attacker will claim 4.2 m)" % (WATER, truth))
    ctx.fault(WATER, "spoof", 4.2, 60)
    v = ctx.wait_for("Guardian untrusted", lambda: untrusted_event(ctx, since, WATER), 15)
    ctx.check("Guardian marked the spoofed sensor untrusted", v, first_reason(v))
    blocked = ctx.wait_for("Brain blocks", lambda: ctx.find(since, "brain/decisions", event="action_blocked", sensor_id=WATER), 25)
    ctx.check("Brain refused to act (action_blocked)", blocked, "; ".join(blocked[0]["msg"].get("reasons", [])[:1]) if blocked else "")
    q = ctx.wait_for("quarantine", lambda: WATER in (ctx.quarantined() or []), 30)
    ctx.check("Guardian quarantined the sensor on the Twin", q, str(ctx.quarantined()))
    ctx.check("Brain sent no command to the valve", not commands(ctx, since, VALVE))
    ctx.check("valve is still closed", ctx.actuator_state(VALVE) in ("closed", "idle"), str(ctx.actuator_state(VALVE)))
    ctx.check("the real water level stayed low (the 4.2 m was fake)", (ctx.truth(WATER) or 9) < 1.0, "%s m" % ctx.truth(WATER))
    ctx.api("DELETE", "/faults/%s" % WATER)
    ctx.check("trust returned (and quarantine was lifted) once the attack ended", trusted_again(ctx, WATER, 60))


# ---------------------------------------------------------------- T4 stuck air sensor
def t4(ctx):
    since = time.time()
    ctx.fault("AIR_01", "stuck", None, 60)
    v = ctx.wait_for("stuck detected", lambda: untrusted_event(ctx, since, "AIR_01"), 40)
    ctx.check("Guardian marked the stuck sensor untrusted", v, first_reason(v))
    ctx.check("the reason names a stuck value", v and "stuck" in first_reason(v), first_reason(v))
    ctx.check("other air sensors stayed trusted", not [x for x in ctx.find(since, "guardian/verdicts/", status="untrusted")
                                                      if x["msg"]["sensor_id"].startswith("AIR_") and x["msg"]["sensor_id"] != "AIR_01"])
    ctx.api("DELETE", "/faults/AIR_01")
    ctx.check("trust returned after the fault ended", trusted_again(ctx, "AIR_01"))


# ---------------------------------------------------------------- T5 replayed readings
def t5(ctx):
    sid = "AIR_04"
    # (a) an attacker re-sends one genuine message, unchanged, 12 s later
    e = ctx.wait_for("a reading to replay", lambda: last_reading(ctx, sid), 10)
    if not e:
        return ctx.check("a reading of %s is available" % sid, False)
    old = dict(e["msg"])
    ctx.step("recorded a genuine %s reading (seq %s); waiting 12 s, then replaying it unchanged" % (sid, old["seq"]))
    time.sleep(12)
    since = time.time()
    ctx.client.publish(e["topic"], json.dumps(old, separators=(",", ":")))
    ctx.attacks.append((sid, since, since + 2))
    v = ctx.wait_for("replay detected", lambda: [x for x in untrusted_event(ctx, since, sid)
                                                 if any("replay" in r or "old" in r for r in x["msg"]["reasons"])], 10)
    ctx.check("Guardian rejected the single replayed message", v, first_reason(v))
    trusted_again(ctx, sid, 20)
    # (b) the Twin's replay fault: a whole stream of old signed readings (old seq and ts)
    since = time.time()
    ctx.fault(sid, "replay", None, 30)
    v = ctx.wait_for("replay stream detected", lambda: [x for x in untrusted_event(ctx, since, sid)
                                                        if any("replay" in r or "old" in r for r in x["msg"]["reasons"])], 15)
    ctx.check("Guardian rejected the replayed stream", v, first_reason(v))
    ctx.check("Brain took no action on the replayed data", not ctx.find(since, "brain/decisions", event="command_sent"))
    ctx.api("DELETE", "/faults/%s" % sid)
    ctx.check("trust returned when genuine readings came back", trusted_again(ctx, sid, 60))


# ---------------------------------------------------------------- T6 real jam + attack, then Guardian killed
def t6(ctx):
    since = time.time()
    ctx.mark("incident")
    # jam=1.0, not 0.95: at 0.95 the --fake cameras sit right at the stand-in Brain's 10 km/h limit (27 vehicles = 11.5 km/h,
    # 28 = 8.4 km/h), so the jammed share keeps dipping under half and the 30 s jam rule restarts (seen in the first run)
    ctx.conditions(zone_id=JAM_ZONE, jam=1.0)
    ctx.step("at the same time an attacker spoofs %s to 4.2 m (fake flood next to the real jam)" % WATER)
    ctx.fault(WATER, "spoof", 4.2, 300)
    sustain = ctx.cfg.get("jam_sustain_s") or 30
    jam = ctx.wait_for("Brain detects the jam", lambda: ctx.find(since, "brain/decisions", event="jam_detected"), sustain + 120)
    ctx.check("Brain detected the real jam", jam, "cameras %s" % (jam[0]["msg"].get("cameras") if jam else "?"))
    sig = ctx.wait_for("signal command", lambda: commands(ctx, since, SIGNAL), 25)
    ctx.check("Brain changed the traffic signal, with a verdict_ref (real data is trusted)",
              sig and sig[0]["msg"].get("verdict_ref"), sig[0]["msg"].get("verdict_ref") if sig else "")
    fb = ctx.wait_for("signal feedback", lambda: feedback(ctx, since, SIGNAL, ok=True), 15)
    ctx.check("signal confirmed its new phase", fb, fb[0]["msg"]["state"] if fb else "no feedback")
    ctx.check("Guardian marked the spoofed gauge untrusted", untrusted_event(ctx, since, WATER))
    ctx.check("Brain blocked the fake flood", ctx.find(since, "brain/decisions", event="action_blocked", sensor_id=WATER))
    ctx.check("no valve command during the attack", not commands(ctx, since, VALVE))

    # --- the Guardian goes down ---
    ctx.mark("guardian_killed")
    killed = ctx.kill_guardian()
    down = ctx.wait_for("Guardian down", lambda: not ctx.guardian_up(), 15)
    ctx.check("Guardian is down", killed and down)
    t_kill = time.time()
    ctx.step("Guardian down: the attacker now spoofs %s to 4.2 m as well" % WATER_UP)
    ctx.fault(WATER_UP, "spoof", 4.2, 120)
    blk = ctx.wait_for("fail closed", lambda: ctx.find(t_kill, "brain/decisions", event="action_blocked", verdict="unreachable"), 40)
    ctx.check("Brain failed closed: tier B action blocked, Guardian unreachable (degraded mode)", blk,
              blk[0]["msg"].get("actuator") if blk else "")
    op = ctx.wait_for("operator request", lambda: ctx.find(t_kill, "brain/decisions", event="operator_request"), 20)
    ctx.check("tier A action (public alert) sent to the operator, not dropped", op, op[0]["msg"].get("request_id") if op else "")
    ctx.check("no valve command while the Guardian was down", not commands(ctx, t_kill, VALVE))
    ctx.check("Twin kept publishing while the Guardian was down", len(ctx.find(t_kill, "city/", type="reading")) > 20)
    try:
        import httpx
        brain_ok = httpx.get(ctx.brain + "/health", timeout=3).status_code == 200 if ctx.brain else False
    except Exception:
        brain_ok = False
    ctx.check("Brain stand-in kept running", brain_ok, only="mocks")

    # --- recovery ---
    ctx.api("DELETE", "/faults/%s" % WATER_UP)
    ctx.api("DELETE", "/faults/%s" % WATER)
    ctx.conditions(zone_id=JAM_ZONE, jam=0.0)
    back = ctx.start_guardian()
    ctx.mark("guardian_back")
    ctx.check("Guardian restarted and answers /health", back, only="mocks")


# ---------------------------------------------------------------- T7 stuck drainage valve
def t7(ctx):
    since = time.time()
    ctx.fault(VALVE, "actuator_stuck", None, 600)
    ctx.rain(60)
    flood = wait_flood(ctx, since)
    ctx.check("Brain detected the flood", flood, "level %s m" % (flood[0]["msg"].get("level_m") if flood else "?"))
    if not flood:
        return
    ctx.check("Brain tried the valve first", ctx.wait_for("valve command", lambda: commands(ctx, since, VALVE), 25))
    bad = ctx.wait_for("valve failure", lambda: feedback(ctx, since, VALVE, ok=False), 15)
    ctx.check("valve reported failure (ok=false)", bad, bad[0]["msg"].get("detail") if bad else "no feedback")
    ctx.check("Brain switched to its fallback", ctx.wait_for("fallback", lambda: ctx.find(since, "brain/decisions", event="fallback"), 30))
    road = ctx.wait_for("road closed", lambda: feedback(ctx, since, ROAD, ok=True, state="closed"), 30)
    ctx.check("Brain closed the road and the Twin confirmed it", road)
    disp = ctx.wait_for("dispatch", lambda: feedback(ctx, since, DISPATCH, ok=True, state="dispatched"), 30)
    sent = commands(ctx, since, DISPATCH)
    ctx.check("civil protection dispatched", disp and sent and sent[0]["msg"]["args"].get("type") == "civil_protection",
              "unit %s, ETA %s s" % (disp[0]["msg"].get("unit_id"), disp[0]["msg"].get("eta_s")) if disp else "")
    ctx.check("public alert was sent", ctx.wait_for("alert", lambda: commands(ctx, since, ALERT), 15))
    ctx.check("valve really stayed closed (the fault is physical)", ctx.actuator_state(VALVE) in ("closed", "idle"),
              str(ctx.actuator_state(VALVE)))
    if disp:
        eta = float(disp[0]["msg"].get("eta_s") or 60)
        arr = ctx.wait_for("unit arrives", lambda: feedback(ctx, since, DISPATCH, state="arrived"), min(360, eta * 1.3 + 30))
        ctx.check("the unit arrived (feedback 'arrived')", arr, arr[0]["msg"].get("detail") if arr else "")
    ctx.rain(0)
    ctx.api("DELETE", "/faults/%s" % VALVE)
    ctx.step("draining: Twin reset to its calm water level")
    t_reset = time.time()
    ctx.api("POST", "/scenario/reset")
    cleared = ctx.wait_for("flood over", lambda: ctx.find(t_reset, "brain/decisions", event="flood_cleared"), 60)
    ctx.check("Brain declared the flood over", cleared)
    reopened = ctx.wait_for("road reopened", lambda: ctx.find(t_reset, "brain/decisions", event="command_sent", actuator=ROAD), 30)
    ctx.check("Brain reopened the road afterwards", reopened)


# ---------------------------------------------------------------- T8 tampered message
def t8(ctx):
    sid = "AIR_04"
    e = ctx.wait_for("a reading to copy", lambda: last_reading(ctx, sid), 10)
    if not e:
        return ctx.check("a reading of %s is available" % sid, False)
    from datetime import datetime, timezone
    forged = dict(e["msg"])
    forged["value"] = (forged["value"] or 0) + 5          # tampered value, signature left as it was
    forged["seq"] += 1000
    forged["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    since = time.time()
    ctx.step("publishing a tampered copy of a %s reading (value changed, signature not updated)" % sid)
    ctx.client.publish(e["topic"], json.dumps(forged, separators=(",", ":")))
    ctx.attacks.append((sid, since, since + 2))
    v = ctx.wait_for("forgery detected", lambda: [x for x in untrusted_event(ctx, since, sid)
                                                  if any("signature" in r for r in x["msg"]["reasons"])], 10)
    ctx.check("Guardian rejected the message: signature invalid", v)
    time.sleep(3)
    ctx.check("the genuine sensor was not quarantined because of the forgery", sid not in (ctx.quarantined() or []))


# ---------------------------------------------------------------- T9 drift (extra)
def t9(ctx):
    sid = "AIR_03"
    since = time.time()
    ctx.fault(sid, "drift", 4.0, 90)          # +4 AQI per simulated second
    v = ctx.wait_for("drift detected", lambda: untrusted_event(ctx, since, sid), 60)
    ctx.check("Guardian flagged the drifting sensor", v, "%s after %.0f s" % (first_reason(v), v[0]["t"] - since) if v else "")
    ctx.api("DELETE", "/faults/%s" % sid)
    ctx.check("trust returned after the fault ended", trusted_again(ctx, sid, 60))


# ---------------------------------------------------------------- T10 network flooding (extra)
def t10(ctx):
    sid = "POWER_02"
    since = time.time()
    ctx.fault(sid, "flood_network", 20, 30)    # 20 extra messages per reading
    v = ctx.wait_for("flooding detected", lambda: untrusted_event(ctx, since, sid), 20)
    ctx.check("Guardian flagged the flooding device", v, first_reason(v))
    ctx.check("the reason names the message rate", v and "rate" in first_reason(v), first_reason(v))
    time.sleep(3)
    others = [x["msg"]["sensor_id"] for x in ctx.find(since, "guardian/verdicts/", status="untrusted") if x["msg"]["sensor_id"] != sid]
    ctx.check("no other sensor was flagged (the flood did not drown the rest)", not others, ", ".join(sorted(set(others))))
    ctx.api("DELETE", "/faults/%s" % sid)
    ctx.check("trust returned after the flood ended", trusted_again(ctx, sid, 60))


# ---------------------------------------------------------------- T11 dropout (extra)
def t11(ctx):
    since = time.time()
    ctx.fault("POWER_01", "dropout", None, 60)
    v = ctx.wait_for("dropout detected", lambda: untrusted_event(ctx, since, "POWER_01"), 20)
    ctx.check("Guardian marked the silent sensor untrusted", v, first_reason(v))
    ctx.api("DELETE", "/faults/POWER_01")
    ctx.check("trust returned when data came back", trusted_again(ctx, "POWER_01", 60))


# ---------------------------------------------------------------- T12 impossible value (extra)
def t12(ctx):
    since = time.time()
    ctx.fault("AIR_03", "spoof", 999, 30)
    v = ctx.wait_for("impossible value", lambda: untrusted_event(ctx, since, "AIR_03"), 15)
    ctx.check("Guardian rejected the impossible value", v, first_reason(v))
    ctx.api("DELETE", "/faults/AIR_03")
    ctx.check("trust returned after the fault ended", trusted_again(ctx, "AIR_03"))


PLAN = "Phase 0 test plan"
SCENARIOS = {
    "t1": dict(
        name="Calm day, no false alarms", layers="Twin, Guardian, Brain", watch=[WATER], run=t1, pdf=PLAN,
        description="Normal running with no rain and no fault for 30 seconds. Shows the layers stay quiet when nothing is wrong.",
        expect=dict(twin="Publishes signed readings about once per second; valve stays closed.",
                    guardian="Every sensor trusted; no untrusted verdict.", brain="No decision taken.")),
    "t2": dict(
        name="Real flood: valve opens, alert sent", layers="Twin, Guardian, Brain", watch=[WATER_UP, WATER], run=t2, pdf=PLAN,
        description="Heavy rain (60 mm/h) raises the canal above the Brain's flood threshold. The Brain must act, because the data is real.",
        expect=dict(twin="Water level rises; VALVE_03 confirms 'open'; public alert shown; valve closes after the flood.",
                    guardian="Water sensors stay trusted (a real flood is not an attack).",
                    brain="flood_detected, verification (trusted), command_sent with verdict_ref (valve open, public alert), "
                          "actuator_result ok, later flood_cleared and valve closed.")),
    "t3": dict(
        name="Spoofed flood: attack is blocked", layers="Twin, Guardian, Brain", watch=[WATER], run=t3, pdf=PLAN,
        description="An attacker makes WATER_08 report 4.2 m while the true level is about 0.4 m. The Brain must NOT open the valve.",
        expect=dict(twin="Publishes 4.2 m labelled fault=spoof; ground truth stays low; valve untouched; sensor quarantined.",
                    guardian="WATER_08 untrusted ('jumped from ... to 4.2'); quarantined after 3 bad windows, released after 3 clean ones.",
                    brain="flood_detected then action_blocked with the Guardian's reason; no valve command.")),
    "t4": dict(
        name="Stuck air sensor detected", layers="Twin, Guardian", watch=["AIR_01"], run=t4, pdf=PLAN,
        description="AIR_01 keeps repeating the same value.",
        expect=dict(twin="Same value repeated, labelled fault=stuck.", guardian="AIR_01 untrusted: 'value unchanged for 10+ readings'.",
                    brain="No decision (air quality is not used by the stand-in Brain).")),
    "t5": dict(
        name="Replayed readings rejected", layers="Twin, Guardian", watch=[], run=t5, pdf=PLAN,
        description="(a) A genuine AIR_04 reading is re-sent unchanged 12 s later; (b) the Twin's replay fault re-publishes a "
                    "stream of old signed AIR_04 readings with their old seq and ts.",
        expect=dict(twin="(b) old signed messages instead of fresh ones, ground truth unchanged.",
                    guardian="AIR_04 untrusted: 'replayed or repeated message' or 'timestamp is N s old'.",
                    brain="No action on the replayed data.")),
    "t6": dict(
        name="Real jam + simultaneous attack, then Guardian killed", layers="Twin, Guardian, Brain", watch=[WATER, WATER_UP], run=t6, pdf=PLAN,
        description="A real jam builds up in zone_a while WATER_08 is spoofed to 4.2 m. The Brain must handle the jam and block the "
                    "fake flood. Then the Guardian process is killed and WATER_07 is spoofed too: the Brain must fail closed "
                    "(degraded mode). Finally the Guardian is restarted.",
        expect=dict(twin="Cameras in zone_a slow down; TL_01 confirms its new phase; keeps publishing while the Guardian is down.",
                    guardian="Cameras trusted, WATER_08 untrusted; then down; then back.",
                    brain="jam_detected, signal command with verdict_ref; action_blocked for WATER_08; after the kill: action_blocked "
                          "verdict=unreachable (tier B valve) and operator_request (tier A alert); no valve command.")),
    "t7": dict(
        name="Stuck drainage valve: Brain closes the road and dispatches", layers="Twin, Guardian, Brain", watch=[WATER], run=t7, pdf=PLAN,
        description="VALVE_03 is stuck (fault actuator_stuck) when a real flood starts. The valve command fails, so the Brain "
                    "closes ROAD_01 and dispatches civil protection instead.",
        expect=dict(twin="VALVE_03 feedback ok=false; ROAD_01 closed; DISPATCH_01 'dispatched' with unit and ETA, then 'arrived'.",
                    guardian="Water sensors stay trusted.",
                    brain="flood_detected, valve command, action_failed, fallback, road closed, civil_protection dispatched, alert; "
                          "after the flood: flood_cleared and road reopened.")),
    "t8": dict(
        name="Tampered message rejected", layers="Twin, Guardian", watch=[], run=t8, pdf="added (integrity check)",
        description="A reading is copied, its value is changed and it is published again with the old signature.",
        expect=dict(twin="Unchanged; it is not the source of this message.", guardian="AIR_04 untrusted: 'signature invalid'; not quarantined.",
                    brain="No decision.")),
    "t9": dict(
        name="Drifting sensor detected", layers="Twin, Guardian", watch=["AIR_03"], run=t9, pdf="added (fault kind drift)",
        description="AIR_03 drifts upwards by 4 AQI per second (fault drift).",
        expect=dict(twin="Value drifts, labelled fault=drift; true value unchanged.",
                    guardian="AIR_03 untrusted once it disagrees with its zone peer AIR_04.", brain="No decision.")),
    "t10": dict(
        name="Network flooding detected", layers="Twin, Guardian", watch=[], run=t10, pdf="added (fault kind flood_network)",
        description="POWER_02 publishes 20 extra messages per reading (fault flood_network).",
        expect=dict(twin="Burst of signed POWER_02 messages.", guardian="POWER_02 untrusted: 'message rate ... (network flooding)'; others unaffected.",
                    brain="No decision.")),
    "t11": dict(
        name="Dropout detected", layers="Twin, Guardian", watch=[], run=t11, pdf="added (fault kind dropout)",
        description="POWER_01 stops sending data.",
        expect=dict(twin="No POWER_01 readings while the fault lasts.", guardian="POWER_01 untrusted: 'no data for N s'.",
                    brain="No decision.")),
    "t12": dict(
        name="Impossible value rejected", layers="Twin, Guardian", watch=["AIR_03"], run=t12, pdf="added (fault kind spoof)",
        description="AIR_03 reports an air quality index of 999, which is above the physical range.",
        expect=dict(twin="Publishes 999 labelled fault=spoof.", guardian="AIR_03 untrusted: 'outside the possible range'.",
                    brain="No decision.")),
}
