"""Guardian STAND-IN (mock), written by the Twin team. Not the real Guardian: simple, explainable rules only.

It lets the Twin (and a Brain) be tested and demonstrated without the Guardian team's code (rule R1), and it follows
EXACTLY the contract the real Guardian must follow (INTEGRATION.md, docs/schemas/verdict.schema.json):

  MQTT in   city/+/+/+                          every reading
  MQTT out  guardian/verdicts/<sensor_id>       a verdict whenever a sensor's status changes
            guardian/verify                     the answer to every POST /verify
            guardian/alerts                     isolated / quarantined / released / watcher
  REST      POST /verify                        {"sensor_ids": [...]} or {"reading_ids": ["WATER_08#42"]} or {"readings": [...]}
                                                -> {"type":"verdict","verdict_id","status","trust" 0..1,"reasons",...}
            GET  /verdict/{sensor_id | reading_id | verdict_id}, GET /verdicts, GET /devices/{id}/status,
            POST /devices/{id}/release (operator), GET /health
  Twin      POST/DELETE /quarantine/<id> with the Guardian's own Twin key, after 3 untrusted windows

Checks (any failure = untrusted)
  signature (checked FIRST; a bad message changes no state) | replayed message (seq goes back) | timestamp more than
  max_age_s old or in the future | message rate (network flooding) | missing or impossible value | jump too big |
  stuck | disagrees with trusted sensors of the same kind in its zone (3 readings) | no data for a few seconds
Graded trust: trusted 1.0; suspicious 0.65 while a sensor starts to disagree with its peers (1-2 readings);
untrusted 0.1 for a failed rule, 0.0 for a failed integrity check. The real Guardian uses the full score formula.

It deliberately IGNORES the `fault` label and the city/faults topic: those reveal the answer.

Run:  python twin/mocks/guardian_mock.py        (MQTT broker running; the Twin for readings and quarantine)
"""
import argparse
import json
import os
import statistics
import threading
import time
import uuid
from collections import OrderedDict, deque

import httpx
import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException

from common import JsonLines, load_config, make_mqtt, now_iso, parse_iso, sign, signature_ok

CONTRACT = "1.0"
RANGES = {"traffic": (0, 130), "traffic_camera": (0, 300), "water_level": (0, 8), "rain": (0, 300),
          "air_quality": (0, 500), "power_load": (0, 115)}
MAX_STEP = {"water_level": 0.3, "air_quality": 150, "power_load": 25, "traffic": 70}   # per reading
PEER_TOLERANCE = {"water_level": 0.8, "air_quality": 60, "power_load": 30, "rain": 40}
STUCK_KINDS = {"water_level", "air_quality", "power_load"}   # kinds that always show a little noise
FLOOR = {"water_level": 0.11}      # a sensor sitting at its lowest possible reading is not necessarily stuck
CEILING = {"water_level": 5.99, "power_load": 109.9}   # nor is one pinned at full scale
MAX_STEPS_ALLOWED = 10             # however long a sensor was untrusted, a jump of more than 10 steps is never "normal"
RANK = {"trusted": 0, "suspicious": 1, "untrusted": 2}
TRUST = {"trusted": 1.0, "suspicious": 0.65, "untrusted": 0.1}


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "schemas")


def schema_problems(name, msg):
    try:
        import jsonschema
        with open(os.path.join(SCHEMA_DIR, name + ".schema.json"), encoding="utf-8") as f:
            v = jsonschema.Draft202012Validator(json.load(f))
    except (ImportError, OSError):
        return []
    return ["%s: %s" % (".".join(map(str, e.path)) or "(body)", e.message) for e in v.iter_errors(msg)]


class Guardian:
    def __init__(self, twin_cfg, cfg, log_dir, connect=True):
        self.twin_cfg, self.cfg = twin_cfg, cfg
        self.key = twin_cfg["api"]["hmac_key"]
        self.lock = threading.RLock()
        self.sensors = {}            # sensor_id -> state
        self.verdicts = {}           # sensor_id -> latest verdict message
        self.verify_cache = OrderedDict()
        self.alerts = deque(maxlen=200)
        self.watcher_tripped = False
        self.log = JsonLines(os.path.join(log_dir, "guardian_verdicts.jsonl"))
        self.alert_log = JsonLines(os.path.join(log_dir, "guardian_alerts.jsonl"))
        self.mqtt = make_mqtt(twin_cfg, self.on_message, ["city/+/+/+"]) if connect else None

    def publish(self, topic, msg, log=None):
        msg["sig"] = sign(msg, self.key)
        if self.mqtt:
            self.mqtt.publish(topic, json.dumps(msg, separators=(",", ":")), qos=1)
        if log:
            log.append(msg)
        return msg

    def alert(self, event, reasons, **extra):
        a = self.publish("guardian/alerts", {"contract_version": CONTRACT, "type": "alert", "event": event,
                                             "reasons": reasons, "ts": now_iso(), **extra}, self.alert_log)
        self.alerts.append(a)
        print("[guardian] ALERT %s %s %s" % (event, extra.get("sensor_id", ""), reasons[:1]))

    # ---------- MQTT ----------
    def on_message(self, client, userdata, message):
        try:
            msg = json.loads(message.payload)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "reading" or msg.get("kind") == "emergency_call":
            return
        try:
            with self.lock:
                self.check(msg)
        except Exception as e:   # a bad message must never stop the Guardian
            print("[guardian] could not check %s: %s" % (msg.get("sensor_id"), e))

    # ---------- the checks ----------
    def trusted_peers(self, sid, zone, kind):
        return [s["base"] for k, s in self.sensors.items()
                if k != sid and s["zone"] == zone and s["kind"] == kind
                and s["status"] == "trusted" and s["base"] is not None]

    def new_state(self, zone, kind):
        return {"zone": zone, "kind": kind, "last_seq": None, "last_ts": None, "base": None, "base_seq": None,
                "last_val": None, "same": 0, "cand": None, "cand_n": 0, "peer_n": 0, "status": None, "reasons": [],
                "seen": time.time(), "times": deque(), "by_seq": OrderedDict(), "quarantined": False,
                "win": {"n": 0, "bad": 0, "start": time.time()}, "bad_windows": 0, "clean_windows": 0, "last_trusted": None}

    def check(self, msg, commit=True):
        """Judge one reading. With commit=False nothing is remembered (used by POST /verify for unseen readings)."""
        sid, kind, zone = msg.get("sensor_id"), msg.get("kind"), msg.get("zone_id")
        v, seq, ts = msg.get("value"), msg.get("seq"), msg.get("ts")
        # The signature is checked FIRST. A message that fails it must not change anything we remember about the
        # sensor (seq, ts, last value, "seen"), otherwise a forged message dated in the future could make genuine
        # readings look replayed, or hide a dropout.
        if not signature_ok(msg, self.key):
            st = self.sensors.get(sid) or {"zone": zone, "kind": kind, "status": None, "reasons": [], "quarantined": False}
            return self.set_verdict(sid, st, "untrusted", ["signature invalid"], seq, msg, integrity=True, commit=commit and sid in self.sensors)
        st = self.sensors.get(sid) or self.new_state(zone, kind)
        if commit:
            self.sensors[sid] = st
        if st["last_seq"] is not None and isinstance(seq, int) and seq <= st["last_seq"]:
            # A replayed message is genuine but old: report it, and leave the sensor's state untouched.
            return self.set_verdict(sid, st, "untrusted", ["replayed or repeated message (seq %s after %s)" % (seq, st["last_seq"])],
                                    seq, msg, integrity=True, commit=commit)
        try:
            age = time.time() - parse_iso(ts).timestamp()
        except (TypeError, ValueError):
            return self.set_verdict(sid, st, "untrusted", ["timestamp missing or malformed"], seq, msg, integrity=True, commit=commit)
        if abs(age) > self.cfg["max_age_s"]:
            return self.set_verdict(sid, st, "untrusted", ["timestamp is %.0f s %s (limit %s s)" % (abs(age), "old" if age > 0 else "in the future",
                                                                                                 self.cfg["max_age_s"])],
                                    seq, msg, integrity=True, commit=commit)
        if not commit:
            st = dict(st)
        st["seen"] = time.time()
        st["last_seq"], st["last_ts"] = seq, ts
        st["times"] = st["times"] if commit else deque(st["times"])
        st["times"].append(time.time())
        while st["times"] and time.time() - st["times"][0] > 5:
            st["times"].popleft()
        bad, soft = [], []
        if len(st["times"]) > 5 * self.cfg.get("max_rate_per_s", 3) + 2:
            bad.append("message rate %.0f/s is far above normal (network flooding)" % (len(st["times"]) / 5))

        numeric = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
        if not numeric:
            bad.append("no numeric value")
        else:
            lo, hi = RANGES.get(kind, (float("-inf"), float("inf")))
            if not lo <= v <= hi:
                bad.append("value %s is outside the possible range %s to %s" % (v, lo, hi))
            else:
                # stuck: the exact same number again and again
                st["same"] = st["same"] + 1 if v == st["last_val"] else 0
                if kind in STUCK_KINDS and st["same"] >= self.cfg["stuck_after"] \
                        and FLOOR.get(kind, float("-inf")) < v < CEILING.get(kind, float("inf")):
                    bad.append("value unchanged for %d+ readings (stuck sensor?)" % self.cfg["stuck_after"])
                # jump: too big a change since the last trusted value
                if st["base"] is not None and kind in MAX_STEP and isinstance(seq, int):
                    allowed = MAX_STEP[kind] * min(MAX_STEPS_ALLOWED, max(1, seq - st["base_seq"]))
                    if abs(v - st["base"]) > allowed:
                        if not self.sustained(st, v, kind, zone, sid):
                            bad.append("jumped from %.3g to %.3g (more than a sensor can change in one step)" % (st["base"], v))
                    else:
                        st["cand"], st["cand_n"] = None, 0
                # peers: sensors of the same kind in the same zone that we trust
                # (3 readings in a row, because sensors in a zone update one after another)
                peers = self.trusted_peers(sid, zone, kind)
                tol = PEER_TOLERANCE.get(kind)
                if peers and tol and abs(v - statistics.median(peers)) > tol:
                    st["peer_n"] += 1
                    gap = abs(v - statistics.median(peers))
                    if st["peer_n"] >= 3:
                        bad.append("disagrees with trusted %s sensors in %s by %.3g" % (kind, zone, gap))
                    else:
                        soft.append("starts to disagree with trusted %s sensors in %s by %.3g" % (kind, zone, gap))
                else:
                    st["peer_n"] = 0
            st["last_val"] = v
        if not bad and numeric:
            st["base"], st["base_seq"] = v, seq
            st["last_trusted"] = {"value": v, "ts": ts, "seq": seq}
        status = "untrusted" if bad else "suspicious" if soft else "trusted"
        return self.set_verdict(sid, st, status, bad or soft, seq, msg, commit=commit)

    def sustained(self, st, v, kind, zone, sid):
        """A big jump becomes the new normal only if it keeps agreeing with itself AND with trusted peers."""
        if st["cand"] is not None and abs(v - st["cand"]) <= MAX_STEP[kind]:
            st["cand_n"] += 1
        else:
            st["cand"], st["cand_n"] = v, 1
        if st["cand_n"] < self.cfg["sustained_change_after"]:
            return False
        peers = self.trusted_peers(sid, zone, kind)
        tol = PEER_TOLERANCE.get(kind)
        if peers and tol and abs(v - statistics.median(peers)) > tol:
            return False
        st["cand"], st["cand_n"] = None, 0
        return True

    # ---------- verdicts ----------
    def make_verdict(self, sid, st, status, reasons, seq, integrity=False):
        trust = 0.0 if integrity else TRUST[status]
        if st.get("quarantined") and status != "untrusted":
            reasons, status, trust = ["quarantined by the Guardian"] + list(reasons), "untrusted", 0.1
        return {"contract_version": CONTRACT, "type": "verdict", "reading_id": "%s#%s" % (sid, seq) if seq is not None else None,
                "sensor_id": sid, "zone_id": st["zone"], "kind": st["kind"], "reading_seq": seq if isinstance(seq, int) else None,
                "status": status, "trust": trust, "reasons": list(reasons),
                "checked_by": ["integrity"] if integrity else ["integrity", "rules", "neighbours"],
                "quarantined": bool(st.get("quarantined")), "ts": now_iso()}

    def set_verdict(self, sid, st, status, reasons, seq, msg=None, integrity=False, commit=True):
        v = self.make_verdict(sid, st, status, reasons, seq, integrity)
        if not commit:
            return v
        raw_bad = status == "untrusted"
        if "win" in st and not integrity:      # forged or replayed messages must not get a genuine sensor quarantined
            st["win"]["n"] += 1
            st["win"]["bad"] += raw_bad
        changed = st["status"] != v["status"] or (v["reasons"][:1] != st["reasons"][:1])
        st["status"], st["reasons"] = v["status"], list(v["reasons"])
        if "by_seq" in st and isinstance(seq, int):
            st["by_seq"][seq] = v
            while len(st["by_seq"]) > 300:
                st["by_seq"].popitem(last=False)
        self.verdicts[sid] = v
        if changed:
            self.publish("guardian/verdicts/%s" % sid, dict(v), self.log)
            print("[guardian] %s -> %s %s" % (sid, v["status"], v["reasons"][:1] or ""))
        return v

    # ---------- periodic: dropout, windows, quarantine ----------
    def tick(self):
        quarantine, release = [], []
        with self.lock:
            now = time.time()
            for sid, st in self.sensors.items():
                quiet = now - st["seen"]
                if quiet > self.cfg["dropout_after_s"] and not (st["status"] == "untrusted" and st["reasons"][:1]
                                                               and "no data" in st["reasons"][0]):
                    self.set_verdict(sid, st, "untrusted", ["no data for %.0f s (dropout?)" % quiet], st["last_seq"])
                if now - st["win"]["start"] >= self.cfg.get("window_s", 5):
                    w = st["win"]
                    if (w["n"] and w["bad"] * 2 >= w["n"]) or (not w["n"] and quiet > self.cfg["dropout_after_s"]):
                        st["bad_windows"], st["clean_windows"] = st["bad_windows"] + 1, 0
                        if st["bad_windows"] >= self.cfg.get("quarantine_after_windows", 3) and not st["quarantined"]:
                            quarantine.append(sid)
                    elif w["n"] and not w["bad"]:
                        st["clean_windows"], st["bad_windows"] = st["clean_windows"] + 1, 0
                        if st["quarantined"] and st["clean_windows"] >= self.cfg.get("release_after_clean_windows", 3):
                            release.append(sid)
                    st["win"] = {"n": 0, "bad": 0, "start": now}
            flagged = [s for s, st in self.sensors.items() if st["status"] == "untrusted"]
            limit = self.cfg.get("max_flagged_fraction", 0.3)
            if self.sensors and len(flagged) / len(self.sensors) > limit and not self.watcher_tripped:
                self.watcher_tripped = True
                self.alert("watcher", ["%d of %d sensors untrusted: possible Guardian fault or city-wide attack; "
                                       "automatic quarantine suspended, operator review needed" % (len(flagged), len(self.sensors))],
                           flagged=sorted(flagged))
            elif self.sensors and len(flagged) / len(self.sensors) <= limit / 2 and self.watcher_tripped:
                self.watcher_tripped = False
                self.alert("watcher_cleared", ["flagged share back to normal; automatic quarantine resumed"])
        for sid in quarantine:
            if self.watcher_tripped:
                self.alert("quarantine_suspended", ["%s would be quarantined, but too many sensors are flagged" % sid], sensor_id=sid)
            elif self.cfg.get("auto_quarantine", True):
                self.quarantine(sid, True)
        for sid in release:
            self.quarantine(sid, False, "readings trusted again for %d windows" % self.cfg.get("release_after_clean_windows", 3))

    def quarantine(self, sid, on, why=None):
        url = "%s/quarantine/%s" % (self.cfg["twin_url"].rstrip("/"), sid)
        try:
            r = httpx.request("POST" if on else "DELETE", url, headers={"X-API-Key": self.cfg["twin_key"]}, timeout=3)
            ok, detail = r.status_code == 200, "HTTP %s" % r.status_code
        except httpx.HTTPError as e:
            ok, detail = False, str(e)
        with self.lock:
            st = self.sensors.get(sid)
            if ok and st:
                st["quarantined"] = on
                st["bad_windows"] = st["clean_windows"] = 0
            event = ("quarantined" if on else "released") if ok else ("quarantine_failed" if on else "release_failed")
            reasons = ([why] if why else list(st["reasons"]) if st else []) + ([] if ok else ["Twin: %s" % detail])
            self.alert(event, reasons, sensor_id=sid, zone_id=st["zone"] if st else None,
                       last_trusted=st.get("last_trusted") if st else None)
        return ok, detail

    def watchdog(self):
        while True:
            time.sleep(1)
            try:
                self.tick()
            except Exception as e:
                print("[guardian] watchdog error: %s" % e)

    # ---------- POST /verify ----------
    def verify(self, body):
        items = []
        for rid in body.get("reading_ids") or []:
            sid, _, seq = rid.rpartition("#")
            v = None
            for _ in range(10):                      # the reading may still be on its way
                with self.lock:
                    v = self.sensors.get(sid, {}).get("by_seq", {}).get(int(seq)) if seq.isdigit() else None
                if v:
                    break
                time.sleep(0.1)
            items.append(v or self.unknown(sid, "reading %s not seen by the Guardian" % rid))
        with self.lock:
            for sid in body.get("sensor_ids") or []:
                items.append(self.verdicts.get(sid) or self.unknown(sid, "no reading of %s seen yet" % sid))
            for msg in body.get("readings") or []:
                items.append(self.check(msg, commit=False))
        if not items:
            raise HTTPException(422, "give at least one of reading_ids, sensor_ids or readings")
        worst = max(items, key=lambda v: (RANK[v["status"]], -v["trust"]))
        out = {"contract_version": CONTRACT, "type": "verdict", "verdict_id": "V-" + uuid.uuid4().hex[:10],
               "reading_ids": [v.get("reading_id") for v in items if v.get("reading_id")],
               "sensor_id": worst.get("sensor_id"), "zone_id": worst.get("zone_id"), "status": worst["status"],
               "trust": min(v["trust"] for v in items),
               "reasons": ["%s: %s" % (v.get("sensor_id"), r) for v in items for r in v.get("reasons", [])[:2]],
               "checked_by": ["integrity", "rules", "neighbours"], "ts": now_iso()}
        out = self.publish("guardian/verify", out, self.log)
        with self.lock:
            self.verify_cache[out["verdict_id"]] = out
            while len(self.verify_cache) > 1000:
                self.verify_cache.popitem(last=False)
        return out

    @staticmethod
    def unknown(sid, reason):
        return {"contract_version": CONTRACT, "type": "verdict", "sensor_id": sid, "status": "untrusted", "trust": 0.0,
                "reasons": [reason], "ts": now_iso()}


def build_app(g):
    app = FastAPI(title="Guardian stand-in", version=CONTRACT)

    def auth(x_api_key: str = Header(default="")):
        if x_api_key != g.cfg["api_key"]:
            raise HTTPException(401, "invalid or missing X-API-Key")

    @app.get("/health")
    def health():
        with g.lock:
            return {"ok": True, "stand_in": True, "contract_version": CONTRACT, "sensors_seen": len(g.verdicts),
                    "untrusted": sorted(s for s, st in g.sensors.items() if st["status"] == "untrusted"),
                    "quarantined": sorted(s for s, st in g.sensors.items() if st["quarantined"]),
                    "watcher_tripped": g.watcher_tripped}

    @app.post("/verify", dependencies=[Depends(auth)])
    def verify(body: dict = Body(...)):
        problems = schema_problems("verify_request", body)
        if problems:
            raise HTTPException(422, "body does not match docs/schemas/verify_request.schema.json: " + "; ".join(problems))
        return g.verify(body)

    @app.get("/verdict/{ref}", dependencies=[Depends(auth)])
    def verdict(ref: str):
        with g.lock:
            if ref in g.verify_cache:
                return g.verify_cache[ref]
            if "#" in ref:
                sid, _, seq = ref.rpartition("#")
                v = g.sensors.get(sid, {}).get("by_seq", {}).get(int(seq)) if seq.isdigit() else None
                if v is None:
                    raise HTTPException(404, "no verdict for reading %s" % ref)
                return v
            if ref not in g.verdicts:
                raise HTTPException(404, "no verdict yet for %s (no reading seen)" % ref)
            return g.verdicts[ref]

    @app.get("/verdicts", dependencies=[Depends(auth)])
    def verdicts():
        with g.lock:
            return list(g.verdicts.values())

    @app.get("/devices/{sid}/status", dependencies=[Depends(auth)])
    def status(sid: str):
        with g.lock:
            st = g.sensors.get(sid)
            if st is None:
                raise HTTPException(404, "unknown sensor %s" % sid)
            return {"sensor_id": sid, "status": st["status"], "reasons": st["reasons"], "quarantined": st["quarantined"],
                    "untrusted_windows": st["bad_windows"], "clean_windows": st["clean_windows"],
                    "last_trusted": st["last_trusted"]}

    @app.post("/devices/{sid}/release", dependencies=[Depends(auth)])
    def release(sid: str):
        """Operator action: release a quarantined sensor now."""
        ok, detail = g.quarantine(sid, False, "released by the operator")
        if not ok:
            raise HTTPException(503, "the Twin did not release %s: %s" % (sid, detail))
        return {"released": sid}

    @app.get("/alerts", dependencies=[Depends(auth)])
    def alerts(limit: int = 50):
        return list(g.alerts)[-max(0, limit):][::-1]

    return app


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--twin-config", default=os.path.join(here, "..", "city.yaml"))
    ap.add_argument("--config", default=os.path.join(here, "mocks.yaml"))
    a = ap.parse_args()
    twin_cfg, cfg = load_config(a.twin_config, a.config)
    gcfg = dict(cfg["guardian"], twin_url=cfg["twin"]["api_url"])
    log_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(a.config)), cfg["brain"]["log_dir"]))
    g = Guardian(twin_cfg, gcfg, log_dir)
    threading.Thread(target=g.watchdog, daemon=True).start()
    print("Guardian stand-in on http://127.0.0.1:%s  (logs: %s)" % (gcfg["port"], log_dir))
    uvicorn.run(build_app(g), host=gcfg["host"], port=int(gcfg["port"]), log_level="warning")


if __name__ == "__main__":
    main()
