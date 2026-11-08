"""City Brain STAND-IN (mock), written by the Twin team. Not the real Brain: two rules, one playbook each, one gate.

It lets the Twin and a Guardian be tested and demonstrated without the Brain team's code (rule R1), and it follows
EXACTLY the contract the real Brain must follow (INTEGRATION.md, docs/schemas/decision.schema.json):
every decision is published on brain/decisions; before EVERY actuator call it asks the Guardian (POST /verify) and
applies the tiered gate of the Phase 0 report (Table 5); every command carries the verdict_ref it relied on.

Rules (thresholds in mocks.yaml; lower than the report's Table 4 so a demo is quick)
  flood  water level >= flood_threshold_m for confirm_readings readings in a row (per sensor)
  jam    at least jam_share of a zone's cameras below jam_speed_kmh and above jam_density_pct for jam_sustain_s
Playbooks (an action counts as done only when the Twin's feedback confirms it; otherwise the next one is tried)
  flood  open the valve serving the sensor   --if it fails-->  close the zone's road AND dispatch civil protection
         public alert (red) in the zone;  when the level is back down: close the valve, reopen the road
  jam    traffic signal of the zone to jam_signal_phase;  public alert (info)
Gate (Table 5)          tier B: valve, road, signal, grid          tier A: alert, dispatch
  trusted               execute                                     execute
  suspicious            hold hold_s, ask once more, then decide     execute and flag to the operator
  untrusted             block, log the reasons                      block, alert the operator
  Guardian unreachable  block (fail closed)                         ask the operator (never silently dropped)
Operator: GET /operator/requests, POST /operator/requests/<id> {"approve": true} on port 8003.

Run:  python twin/mocks/brain_mock.py       (Twin, MQTT broker and a Guardian should be running; it keeps running
                                              and logging if the Guardian or the Twin drops: degraded mode)
"""
import argparse
import json
import os
import threading
import time
import uuid
from collections import deque

import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException

from common import JsonLines, load_config, make_mqtt, now_iso, sign

CONTRACT = "1.0"


class Brain:
    def __init__(self, twin_cfg, cfg, twin_url, log_dir, connect=True, devices=None):
        self.twin_cfg, self.cfg, self.twin_url = twin_cfg, cfg, twin_url.rstrip("/")
        self.key = twin_cfg["api"]["hmac_key"]
        self.api_headers = {"X-API-Key": cfg.get("twin_key") or twin_cfg["api"]["api_key"]}
        self.lock = threading.RLock()
        self.log = JsonLines(os.path.join(log_dir, "brain_decisions.jsonl"))
        self.decisions = deque(maxlen=500)
        self.devices = devices if devices is not None else self.wait_for_devices()
        self.water = {}              # sensor_id -> counters and incident
        self.cams = {}               # camera id -> (zone, speed, density, time)
        self.jams = {}               # zone -> {"since", "clear_since", "incident"}
        self.zone_flood = {}         # zone -> open flood incident (one per zone; other gauges join it)
        self.cmd_n = self.incident_n = 0
        self.pending = {}            # cmd_id -> {"event", "msg", "incident", "actuator"}
        self.operator = {}           # request id -> pending operator request
        self.mqtt = make_mqtt(twin_cfg, self.on_message, ["city/+/water_level/+", "city/+/traffic_camera/+",
                                                          "city/+/actuator/+/state"]) if connect else None

    def wait_for_devices(self):
        while True:
            try:
                r = httpx.get(self.twin_url + "/devices", timeout=3)
                r.raise_for_status()
                return {d["id"]: d for d in r.json()}
            except (httpx.HTTPError, ValueError) as e:
                print("[brain] waiting for the Twin at %s (%s)" % (self.twin_url, e))
                time.sleep(2)

    def device_in_zone(self, typ, zone):
        ids = sorted(i for i, d in self.devices.items() if d["type"] == typ and d["zone"] == zone)
        return ids[0] if ids else None

    def tier(self, actuator):
        return self.cfg.get("tiers", {}).get(self.devices.get(actuator, {}).get("type"), "B")

    # ---------- decisions ----------
    def record(self, event, incident_id, sensor_id=None, **detail):
        d = {"contract_version": CONTRACT, "type": "decision", "event": event, "incident_id": incident_id,
             "sensor_id": sensor_id, "ts": now_iso(), **detail}
        d["sig"] = sign(d, self.key)
        self.log.append(d)
        self.decisions.append(d)
        if self.mqtt:
            self.mqtt.publish("brain/decisions", json.dumps(d, separators=(",", ":")), qos=1)
        print("[brain] %-16s %-8s %s" % (event, sensor_id or "", {k: v for k, v in detail.items() if k not in ("reasons",)}))
        return d

    def ask_guardian(self, sensors, purpose):
        """(status, trust, reasons, verdict_id). Anything but a clear answer is 'unreachable'."""
        try:
            r = httpx.post(self.cfg["guardian_url"].rstrip("/") + "/verify", timeout=3,
                           headers={"X-API-Key": self.cfg["guardian_key"]},
                           json={"contract_version": CONTRACT, "type": "verify_request", "sensor_ids": sensors, "purpose": purpose})
        except httpx.HTTPError as e:
            return "unreachable", None, ["Guardian not reachable: %s" % type(e).__name__], None
        if r.status_code != 200:
            return "unreachable", None, ["Guardian answered HTTP %s: %s" % (r.status_code, r.text[:100])], None
        v = r.json()
        if v.get("status") not in ("trusted", "suspicious", "untrusted"):
            return "unreachable", None, ["Guardian answer has no valid status"], None
        return v["status"], v.get("trust"), v.get("reasons", []), v.get("verdict_id")

    def gate(self, inc, kind, actuator, args, sensors):
        """Table 5. Returns ("execute", verdict_ref) or ("block" | "operator", None)."""
        tier = self.tier(actuator)
        purpose = "%s: %s %s" % (kind, actuator, json.dumps(args))
        status, trust, reasons, ref = self.ask_guardian(sensors, purpose)
        base = dict(incident=kind, actuator=actuator, args=args, tier=tier, sensor_ids=sensors)
        self.record("verification", inc, sensors[0] if sensors else None, verdict=status, trust=trust, reasons=reasons,
                    verdict_ref=ref, **base)
        if status == "suspicious" and tier == "B":
            self.record("action_held", inc, sensors[0], verdict=status, trust=trust, reasons=reasons, verdict_ref=ref,
                        why="tier B on suspicious data: holding %s s, then asking once more" % self.cfg["hold_s"], **base)
            time.sleep(self.cfg["hold_s"])
            status, trust, reasons, ref = self.ask_guardian(sensors, purpose + " (second ask)")
            self.record("verification", inc, sensors[0], verdict=status, trust=trust, reasons=reasons, verdict_ref=ref,
                        second_ask=True, **base)
            if status == "suspicious":
                status = "untrusted"
                reasons = ["still suspicious after a second ask"] + reasons
        if status == "trusted" or (status == "suspicious" and tier == "A"):
            if status == "suspicious":
                self.record("operator_flag", inc, sensors[0], verdict=status, trust=trust, reasons=reasons, verdict_ref=ref,
                            why="tier A executed on suspicious data; the operator is told", **base)
            return "execute", ref
        if status == "untrusted":
            self.record("action_blocked", inc, sensors[0] if sensors else None, verdict=status, trust=trust, reasons=reasons,
                        verdict_ref=ref, why="the readings behind this decision are not trusted, so no actuator was used", **base)
            if tier == "A":
                self.record("operator_alert", inc, sensors[0], verdict=status, reasons=reasons,
                            why="life-safety action blocked on untrusted data; the operator may override", **base)
            return "block", None
        # Guardian unreachable: degraded mode
        if tier == "B":
            self.record("action_blocked", inc, sensors[0] if sensors else None, verdict="unreachable", reasons=reasons,
                        why="Guardian unreachable: infrastructure actions fail closed (degraded mode)", **base)
            return "block", None
        key = (kind, actuator, tuple(sensors))
        if not any(r["key"] == key and r["state"] == "pending" for r in self.operator.values()):
            rid = "OP-" + uuid.uuid4().hex[:6]
            self.operator[rid] = {"id": rid, "key": key, "state": "pending", "incident": inc, "actuator": actuator,
                                  "args": args, "sensors": sensors, "kind": kind, "ts": now_iso()}
            self.record("operator_request", inc, sensors[0] if sensors else None, verdict="unreachable", reasons=reasons,
                        request_id=rid, why="Guardian unreachable: life-safety action sent to the operator, never dropped", **base)
        return "operator", None

    def command(self, inc, actuator, args, verdict_ref, sensors):
        self.cmd_n += 1
        cmd_id = "brain-%d-%s" % (self.cmd_n, uuid.uuid4().hex[:4])
        wait = {"event": threading.Event(), "msg": None, "incident": inc, "actuator": actuator}
        with self.lock:
            self.pending[cmd_id] = wait
        body = {"contract_version": CONTRACT, "type": "command", "cmd_id": cmd_id, "args": args,
                "verdict_ref": verdict_ref, "ts": now_iso()}
        try:
            r = httpx.post("%s/actuators/%s" % (self.twin_url, actuator), headers=self.api_headers, timeout=3, json=body)
        except httpx.HTTPError as e:
            self.record("command_failed", inc, actuator=actuator, detail="Twin unreachable: %s" % type(e).__name__)
            return None, wait
        if r.status_code != 200:
            self.record("command_failed", inc, actuator=actuator, http=r.status_code, detail=r.text[:200])
            return None, wait
        self.record("command_sent", inc, sensors[0] if sensors else None, actuator=actuator, cmd_id=cmd_id, args=args,
                    tier=self.tier(actuator), verdict_ref=verdict_ref, sensor_ids=sensors)
        return cmd_id, wait

    def act(self, inc, kind, actuator, args, sensors):
        """Gate, command, and wait for confirming feedback. Returns done | blocked | operator | failed."""
        if actuator is None:
            self.record("no_actuator", inc, sensors[0] if sensors else None, incident=kind, why="no actuator of that kind in the zone")
            return "failed"
        decision, ref = self.gate(inc, kind, actuator, args, sensors)
        if decision != "execute":
            return "blocked" if decision == "block" else "operator"
        return self.execute(inc, actuator, args, ref, sensors)

    def execute(self, inc, actuator, args, ref, sensors):
        cmd_id, wait = self.command(inc, actuator, args, ref, sensors)
        if cmd_id is None:
            return "failed"
        if not wait["event"].wait(self.cfg["feedback_timeout_s"]):
            self.record("action_failed", inc, actuator=actuator, cmd_id=cmd_id,
                        why="no feedback within %s s" % self.cfg["feedback_timeout_s"])
            return "failed"
        fb = wait["msg"]
        if not fb.get("ok"):
            self.record("action_failed", inc, actuator=actuator, cmd_id=cmd_id, why=fb.get("detail") or "actuator reported failure")
            return "failed"
        return "done"

    # ---------- flood ----------
    def flood(self, sid, level):
        zone = self.devices.get(sid, {}).get("zone")
        with self.lock:
            st = self.water[sid]
            if self.zone_flood.get(zone):                 # another gauge of this zone already opened the incident
                st["incident"], st["joined"] = self.zone_flood[zone], True
                self.record("flood_detected", st["incident"], sid, incident="flood", zone_id=zone, level_m=level,
                            why="joins the open incident of this zone")
                return
            self.incident_n += 1
            inc = "FLOOD-%03d" % self.incident_n
            st["incident"], st["joined"] = inc, False
            self.zone_flood[zone] = inc
        self.record("flood_detected", inc, sid, incident="flood", zone_id=zone, level_m=level)
        valve = self.devices.get(sid, {}).get("valve") or self.device_in_zone("water_valve", zone)
        outcome = self.act(inc, "flood", valve, {"state": "open"}, [sid])
        if outcome == "done":
            st["valve"] = valve
        elif outcome == "failed":
            self.record("fallback", inc, sid, incident="flood", why="valve did not confirm: closing the road and dispatching civil protection")
            road = self.device_in_zone("road_closure", zone)
            if self.act(inc, "flood", road, {"state": "closed"}, [sid]) == "done":
                st["road"] = road
            self.act(inc, "flood", self.device_in_zone("emergency_dispatch", "city"), {"type": "civil_protection", "zone_id": zone}, [sid])
        alert = self.act(inc, "flood", self.device_in_zone("public_alert", zone),
                         {"level": "red", "msg": "Flood risk near %s (%.2f m). Avoid the area." % (sid, level)}, [sid])
        if outcome == "blocked" or (outcome == "operator" and alert != "done"):
            with self.lock:                          # nothing was done: re-check later
                st["incident"] = None
                self.zone_flood[zone] = None
                st["next_check"] = time.time() + self.cfg["retry_blocked_after_s"]

    def flood_clear(self, sid, level):
        st = self.water[sid]
        inc = st["incident"]
        if st.get("joined"):
            with self.lock:
                st["incident"], st["clearing"] = None, False
            return
        self.record("flood_cleared", inc, sid, incident="flood", level_m=level)
        self.zone_flood[self.devices.get(sid, {}).get("zone")] = None
        if st.get("valve"):
            if self.act(inc, "flood_cleared", st["valve"], {"state": "closed"}, [sid]) == "done":
                st["valve"] = None
        if st.get("road"):
            if self.act(inc, "flood_cleared", st["road"], {"state": "open"}, [sid]) == "done":
                st["road"] = None
        with self.lock:
            st["incident"], st["clearing"] = None, False

    # ---------- jam ----------
    def jam(self, zone, cams):
        with self.lock:
            self.incident_n += 1
            inc = "JAM-%03d" % self.incident_n
            self.jams[zone]["incident"] = inc
        self.record("jam_detected", inc, cams[0], incident="jam", zone_id=zone, cameras=cams)
        signal = self.device_in_zone("traffic_signal", zone)
        out = self.act(inc, "jam", signal, {"phase": self.cfg["jam_signal_phase"]}, cams)
        self.act(inc, "jam", self.device_in_zone("public_alert", zone), {"level": "info", "msg": "Heavy traffic in %s: expect delays." % zone}, cams)
        if out == "blocked":
            with self.lock:
                self.jams[zone].update(incident=None, since=time.time() + self.cfg["retry_blocked_after_s"] - self.cfg["jam_sustain_s"])

    def check_jam(self, zone):
        now = time.time()
        cams = [(c, v) for c, v in self.cams.items() if v[0] == zone and now - v[3] < 5]
        if not cams:
            return
        jammed = [c for c, (z, sp, dens, t) in cams if sp is not None and dens is not None
                  and sp < self.cfg["jam_speed_kmh"] and dens > self.cfg["jam_density_pct"]]
        j = self.jams.setdefault(zone, {"since": None, "clear_since": None, "incident": None})
        if len(jammed) >= self.cfg["jam_share"] * len(cams):
            j["clear_since"] = None
            j["since"] = j["since"] or now
            if not j["incident"] and now - j["since"] >= self.cfg["jam_sustain_s"]:
                j["incident"] = "starting"
                threading.Thread(target=self.jam, args=(zone, sorted(jammed)[:5]), daemon=True).start()
        else:
            j["since"] = None
            if j["incident"] and j["incident"] != "starting":
                j["clear_since"] = j["clear_since"] or now
                if now - j["clear_since"] >= self.cfg["jam_clear_s"]:
                    self.record("jam_cleared", j["incident"], incident="jam", zone_id=zone)
                    j["incident"], j["clear_since"] = None, None

    # ---------- MQTT ----------
    def on_message(self, client, userdata, message):
        try:
            msg = json.loads(message.payload)
            with self.lock:
                if msg.get("type") == "reading" and not msg.get("quarantined"):     # quarantined = isolated input
                    if msg.get("kind") == "water_level":
                        self.on_water(msg)
                    elif msg.get("kind") == "traffic_camera":
                        self.cams[msg["sensor_id"]] = (msg["zone_id"], msg.get("mean_speed_kmh"), msg.get("density_pct"), time.time())
                        self.check_jam(msg["zone_id"])
                elif msg.get("type") == "feedback":
                    self.on_feedback(msg)
        except Exception as e:
            print("[brain] error: %s" % e)

    def on_water(self, msg):
        sid, level = msg["sensor_id"], msg.get("value")
        if not isinstance(level, (int, float)):
            return
        st = self.water.setdefault(sid, {"over": 0, "under": 0, "incident": None, "next_check": 0, "clearing": False})
        if level >= self.cfg["flood_threshold_m"]:
            st["over"], st["under"] = st["over"] + 1, 0
        elif level <= self.cfg["clear_threshold_m"]:
            st["under"], st["over"] = st["under"] + 1, 0
        else:
            st["over"] = 0
        if time.time() < st["next_check"]:
            return
        if st["over"] >= self.cfg["confirm_readings"] and not st["incident"]:
            st["incident"] = "starting"
            threading.Thread(target=self.flood, args=(sid, level), daemon=True).start()
        elif st["incident"] not in (None, "starting") and st["under"] >= self.cfg["clear_readings"] and not st["clearing"]:
            st["clearing"] = True
            threading.Thread(target=self.flood_clear, args=(sid, level), daemon=True).start()

    def on_feedback(self, msg):
        wait = self.pending.pop(msg.get("cmd_id"), None)
        if wait:
            self.record("actuator_result", wait["incident"], actuator=msg.get("actuator_id"), cmd_id=msg["cmd_id"],
                        ok=msg.get("ok"), state=msg.get("state"), detail=msg.get("detail", ""))
            wait["msg"] = msg
            wait["event"].set()


def build_app(b):
    app = FastAPI(title="City Brain stand-in", version=CONTRACT)

    @app.get("/health")
    def health():
        return {"ok": True, "stand_in": True, "contract_version": CONTRACT,
                "open_incidents": sorted({s["incident"] for s in b.water.values() if s["incident"]} |
                                         {j["incident"] for j in b.jams.values() if j["incident"]}),
                "pending_operator_requests": sum(1 for r in b.operator.values() if r["state"] == "pending")}

    @app.get("/decisions")
    def decisions(limit: int = 50):
        return list(b.decisions)[-max(0, limit):][::-1]

    @app.get("/operator/requests")
    def requests():
        return [{k: v for k, v in r.items() if k != "key"} for r in b.operator.values()]

    @app.post("/operator/requests/{rid}")
    def decide(rid: str, body: dict = Body(...)):
        """The operator approves (executes without a verdict, verdict_ref = operator:<id>) or rejects a request."""
        r = b.operator.get(rid)
        if r is None or r["state"] != "pending":
            raise HTTPException(404, "no pending operator request %s" % rid)
        approve = bool(body.get("approve"))
        r["state"] = "approved" if approve else "rejected"
        b.record("operator_decision", r["incident"], r["sensors"][0] if r["sensors"] else None, request_id=rid,
                 approved=approve, actuator=r["actuator"], tier=b.tier(r["actuator"]))
        if approve:
            threading.Thread(target=b.execute, args=(r["incident"], r["actuator"], r["args"], "operator:%s" % rid, r["sensors"]),
                             daemon=True).start()
        return {"request_id": rid, "state": r["state"]}

    return app


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--twin-config", default=os.path.join(here, "..", "city.yaml"))
    ap.add_argument("--config", default=os.path.join(here, "mocks.yaml"))
    a = ap.parse_args()
    twin_cfg, cfg = load_config(a.twin_config, a.config)
    log_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(a.config)), cfg["brain"]["log_dir"]))
    b = Brain(twin_cfg, cfg["brain"], cfg["twin"]["api_url"], log_dir)
    print("Brain stand-in running (REST http://127.0.0.1:%s, logs: %s)" % (cfg["brain"]["port"], log_dir))
    uvicorn.run(build_app(b), host=cfg["brain"]["host"], port=int(cfg["brain"]["port"]), log_level="warning")


if __name__ == "__main__":
    main()
