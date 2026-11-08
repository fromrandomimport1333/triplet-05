"""Contract check: does a layer speak the triplet's contract? Run it against YOUR layer (or ours) before integrating.

    python twin/tools/contract_check.py                              listen 30 s, check the Twin and the Guardian
    python twin/tools/contract_check.py --provoke --seconds 60       also make something happen (a short spoofed
                                                                     reading, needs the Twin's test key), so verdicts,
                                                                     decisions, acks and feedback appear
    python twin/tools/contract_check.py --guardian http://10.0.0.5:8001 --guardian-key KEY --brain http://10.0.0.6:8003

What it does
  1. Subscribes to city/#, guardian/# and brain/# and validates every message against docs/schemas/ by its "type"
     (reading, ack, feedback, verdict, decision, error); with --hmac-key it also checks every signature.
  2. Calls the REST endpoints: Twin GET /health, GET /contract and an unauthorised command (must be a 401 error
     message); Guardian POST /verify (must return a verdict: status trusted/suspicious/untrusted, trust 0..1,
     reasons, verdict_id), GET /verdict/<sensor> and a malformed /verify (must be refused); Brain GET /health.
  3. Prints PASS / FAIL per message type and per call. Exit code 0 only if nothing failed.
Settings: arguments, else environment variables (TWIN_URL, GUARDIAN_URL, BRAIN_URL, GUARDIAN_API_KEY, TWIN_TEST_KEY,
MQTT_HOST, MQTT_PORT, TSYP_HMAC_KEY), else the development defaults.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from collections import defaultdict

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMAS = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "schemas"))
CHECKED_TYPES = ("reading", "ack", "feedback", "verdict", "decision", "error")
INFO_TYPES = ("fault_event", "alert")      # no schema in the contract: listed, not judged


def load_validators():
    import jsonschema
    out = {}
    for name in os.listdir(SCHEMAS):
        if name.endswith(".schema.json"):
            with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
                out[name[:-12]] = jsonschema.Draft202012Validator(json.load(f))
    return out


def sig_ok(msg, key):
    body = json.dumps({k: v for k, v in msg.items() if k != "sig"}, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(msg.get("sig"), str) and hmac.compare_digest(msg["sig"], hmac.new(key.encode(), body, hashlib.sha256).hexdigest())


class Checker:
    def __init__(self, a):
        self.a, self.v = a, load_validators()
        self.results = defaultdict(lambda: {"pass": 0, "fail": 0, "examples": [], "topics": set()})
        self.calls = []
        self.lock = threading.Lock()
        self.seen_sensor = None

    def judge(self, label, msg, topic=None):
        problems = []
        name = msg.get("type") if label is None else label
        if name in self.v:
            problems += ["%s: %s" % (".".join(map(str, e.path)) or "(message)", e.message) for e in self.v[name].iter_errors(msg)]
        if self.a.hmac_key and "sig" in self.v.get(name, type("x", (), {"schema": {}})).schema.get("required", []) and not sig_ok(msg, self.a.hmac_key):
            problems.append("signature does not verify with the given --hmac-key")
        if name == "verdict":
            t = msg.get("trust")
            if not isinstance(t, (int, float)) or not 0 <= t <= 1:
                problems.append("trust must be a number from 0 to 1")
        r = self.results[name]
        with self.lock:
            if topic:
                r["topics"].add(topic.rsplit("/", 1)[0] + "/..." if topic.count("/") >= 2 else topic)
            if problems:
                r["fail"] += 1
                if len(r["examples"]) < 3:
                    r["examples"].append("%s: %s" % (topic or "REST", problems[0]))
            else:
                r["pass"] += 1
        return problems

    def on_message(self, client, userdata, m):
        try:
            msg = json.loads(m.payload)
        except ValueError:
            with self.lock:
                self.results["(not JSON)"]["fail"] += 1
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        if t == "reading" and msg.get("kind") != "emergency_call":
            self.seen_sensor = msg.get("sensor_id")
        if t in CHECKED_TYPES:
            self.judge(None, msg, m.topic)
        elif t in INFO_TYPES:
            with self.lock:
                self.results[t]["pass"] += 1
                self.results[t]["topics"].add(m.topic)
        else:
            with self.lock:
                self.results["(unknown type %s)" % t]["fail"] += 1
                self.results["(unknown type %s)" % t]["examples"].append(m.topic)

    def call(self, name, fn):
        try:
            problems = fn()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            problems = ["%s: %s" % (type(e).__name__, e)]
        self.calls.append((name, problems))

    # ---------- REST checks ----------
    def twin_checks(self):
        t = self.a.twin.rstrip("/")

        def health():
            b = httpx.get(t + "/health", timeout=3).json()
            return [] if b.get("ok") and b.get("contract_version") else ["/health must return ok and contract_version"]

        def contract():
            b = httpx.get(t + "/contract", timeout=3).json()
            need = {"contract_version", "mqtt", "endpoints", "schemas"}
            return [] if need <= set(b) else ["/contract misses %s" % sorted(need - set(b))]

        def unauthorised():
            r = httpx.post(t + "/actuators/VALVE_03", json={"cmd_id": "contract-check", "args": {"state": "open"}}, timeout=3)
            if r.status_code != 401:
                return ["a command without a key must get 401, got %s" % r.status_code]
            return self.judge("error", r.json())

        self.call("Twin GET /health", health)
        self.call("Twin GET /contract", contract)
        self.call("Twin refuses a command without a key (401 + error message)", unauthorised)

    def guardian_checks(self):
        g, h = self.a.guardian.rstrip("/"), {"X-API-Key": self.a.guardian_key}
        sensor = self.seen_sensor or "WATER_08"

        def verify():
            r = httpx.post(g + "/verify", headers=h, timeout=5,
                           json={"contract_version": "1.0", "type": "verify_request", "sensor_ids": [sensor], "purpose": "contract check"})
            if r.status_code != 200:
                return ["POST /verify answered HTTP %s" % r.status_code]
            b = r.json()
            p = self.judge("verdict", b)
            return p + ([] if b.get("verdict_id") else ["the /verify answer needs a verdict_id (the Brain puts it in verdict_ref)"])

        def verdict():
            r = httpx.get("%s/verdict/%s" % (g, sensor), headers=h, timeout=3)
            return ["GET /verdict/%s answered HTTP %s" % (sensor, r.status_code)] if r.status_code != 200 else self.judge("verdict", r.json())

        def malformed():
            r = httpx.post(g + "/verify", headers=h, json={"sensor_ids": "not-a-list"}, timeout=3)
            return [] if 400 <= r.status_code < 500 else ["a malformed /verify must be refused with 4xx, got %s" % r.status_code]

        def no_key():
            r = httpx.post(g + "/verify", json={"sensor_ids": [sensor]}, timeout=3)
            return [] if r.status_code in (401, 403) else ["/verify without a key must get 401 or 403, got %s" % r.status_code]

        self.call("Guardian POST /verify returns a verdict", verify)
        self.call("Guardian GET /verdict/<sensor>", verdict)
        self.call("Guardian refuses a malformed /verify", malformed)
        self.call("Guardian refuses /verify without a key", no_key)

    def brain_checks(self):
        self.call("Brain GET /health", lambda: [] if httpx.get(self.a.brain.rstrip("/") + "/health", timeout=3).json().get("ok")
                  else ["/health must return ok"])

    def provoke(self):
        h = {"X-API-Key": self.a.twin_test_key}
        r = httpx.post(self.a.twin.rstrip("/") + "/faults", headers=h, timeout=3,
                       json={"sensor_id": "WATER_08", "kind": "spoof", "value": 4.2, "duration_s": 15})
        print("provoke: spoofed WATER_08 = 4.2 m for 15 s (HTTP %s)" % r.status_code)


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--twin", default=env("TWIN_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--guardian", default=env("GUARDIAN_URL", "http://127.0.0.1:8001"))
    ap.add_argument("--brain", default=env("BRAIN_URL", "http://127.0.0.1:8003"))
    ap.add_argument("--guardian-key", default=env("GUARDIAN_API_KEY", "guardian-key"))
    ap.add_argument("--twin-test-key", default=env("TWIN_TEST_KEY", "test-key"))
    ap.add_argument("--mqtt-host", default=env("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    ap.add_argument("--hmac-key", default=env("TSYP_HMAC_KEY", "change-me-too"), help="empty string = do not check signatures")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--provoke", action="store_true")
    ap.add_argument("--skip", default="", help="comma list of layers not to call over REST: twin,guardian,brain")
    a = ap.parse_args()
    import paho.mqtt.client as mqtt
    ck = Checker(a)
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = lambda cl, u, f, rc, *r: [cl.subscribe(t) for t in ("city/#", "guardian/#", "brain/#")]
    c.on_message = ck.on_message
    c.connect(a.mqtt_host, a.mqtt_port, 30)
    c.loop_start()
    skip = set(a.skip.split(",")) if a.skip else set()
    print("listening for %.0f s on %s:%s ..." % (a.seconds, a.mqtt_host, a.mqtt_port))
    time.sleep(min(3, a.seconds))
    if a.provoke and "twin" not in skip:
        ck.provoke()
    time.sleep(max(0, a.seconds - 3))
    if "twin" not in skip:
        ck.twin_checks()
    if "guardian" not in skip:
        ck.guardian_checks()
    if "brain" not in skip:
        ck.brain_checks()
    time.sleep(1)
    c.loop_stop()

    failed = False
    print("\nMQTT messages by type")
    for name in sorted(ck.results):
        r = ck.results[name]
        ok = r["fail"] == 0 and r["pass"] > 0
        label = "INFO" if name in INFO_TYPES else ("PASS" if ok else "FAIL")
        failed |= label == "FAIL"
        print("  %s  %-12s %5d valid  %3d invalid   %s" % (label, name, r["pass"], r["fail"], ", ".join(sorted(r["topics"]))[:80]))
        for e in r["examples"]:
            print("          %s" % e[:150])
    for name in CHECKED_TYPES:
        if name not in ck.results and name != "error":
            print("  ----  %-12s not seen (nothing published it during the run; try --provoke or a longer --seconds)" % name)
    print("\nREST calls")
    for name, problems in ck.calls:
        failed |= bool(problems)
        print("  %s  %s%s" % ("FAIL" if problems else "PASS", name, ("  -> " + "; ".join(problems[:2])) if problems else ""))
    print("\nRESULT: %s" % ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
