"""Stand-ins that speak the REAL City Brain protocol (MQTT, signed envelopes), so the Twin can be demonstrated alone with
the integration profile on, without the partners' code. They follow `brain/INTERFACE.md` and the Guardian interface v1.3
and are deliberately small: they are NOT the real layers.

    python twin/mocks/partner_mock.py guardian     # verdicts, quarantine, status  (reads city/#, twin/#)
    python twin/mocks/partner_mock.py brain        # detects a flood, sends brain/cmd, reads the acknowledgements
    python twin/mocks/partner_mock.py both

Settings: `wire:` in mocks.yaml (broker, accounts, key files, thresholds); environment variables override
(PARTNER_MQTT_HOST, PARTNER_MQTT_PORT, PARTNER_MQTT_CA_CERT, PARTNER_MQTT_TLS, TWIN_BRAIN_KEY_FILE, TWIN_GUARDIAN_KEY_FILE,
GUARDIAN_BRAIN_KEY_FILE). The older REST stand-ins (guardian_mock.py, brain_mock.py) remain for the Twin's native mode.
"""
import json
import math
import os
import ssl
import sys
import threading
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from protocol import ProtocolError, iso_ms, open_envelope, read_key, seal  # noqa: E402


def load(path=None):
    with open(path or os.path.join(HERE, "mocks.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["wire"]
    env = os.environ
    for var, (sec, key) in {"PARTNER_MQTT_HOST": ("mqtt", "host"), "PARTNER_MQTT_PORT": ("mqtt", "port"),
                            "PARTNER_MQTT_CA_CERT": ("mqtt", "ca_cert"), "TWIN_BRAIN_KEY_FILE": ("keys", "twin_brain"),
                            "TWIN_GUARDIAN_KEY_FILE": ("keys", "twin_guardian"),
                            "GUARDIAN_BRAIN_KEY_FILE": ("keys", "guardian_brain")}.items():
        if env.get(var):
            cfg[sec][key] = int(env[var]) if key == "port" else env[var]
    if env.get("PARTNER_MQTT_TLS"):
        cfg["mqtt"]["tls"] = env["PARTNER_MQTT_TLS"].lower() in ("1", "true", "yes")
    return cfg


def path(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))


class Peer:
    """One MQTT connection with its keys, shared by both stand-ins."""

    def __init__(self, cfg, name, will_topic):
        self.cfg, self.name, self.n = cfg, name, 0
        self.run = "%s%d" % (name[:2], int(time.time()))
        k = cfg["keys"]
        self.twin_brain = read_key(path(k["twin_brain"]))
        self.twin_guardian = read_key(path(k["twin_guardian"]))
        self.guardian_brain = read_key(path(k["guardian_brain"]))
        m = cfg["mqtt"]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="%s-mock-%s" % (name, self.run))
        if m.get("tls", True):
            self.client.tls_set(ca_certs=path(m["ca_cert"]), cert_reqs=ssl.CERT_REQUIRED)
        if m.get("username"):
            self.client.username_pw_set(m["username"][name], open(path(m["password_file"][name])).read().strip())
        self.client.will_set(will_topic, seal(self.body("status", status="offline"), {"x": self.guardian_brain}), 1, True)
        self.will_topic = will_topic
        self.host, self.port = m["host"], int(m["port"])

    def body(self, prefix, **fields):
        self.n += 1
        return dict({"id": "%s-%s-%06d" % (self.run, prefix, self.n), "timestamp": iso_ms(), "source": self.name}, **fields)

    def start(self, on_connect, on_message):
        self.client.on_connect = lambda c, u, f, rc, p=None: on_connect()
        self.client.on_message = lambda c, u, m: on_message(m)
        self.client.connect(self.host, self.port, 30)
        self.client.loop_start()


class Guardian:
    """Verdicts per the interface v1.3: range from meta, strictly increasing seq, replayed id, jump, frozen (8 identical)."""

    def __init__(self, cfg):
        self.c, self.p = cfg["guardian"], Peer(cfg, "guardian", "guardian/status")
        self.meta, self.seq, self.ids = {}, {}, {}
        self.last = {}                                    # sensor -> last accepted value
        self.hist = defaultdict(lambda: deque(maxlen=self.c["frozen_after"]))
        self.rejected_run = defaultdict(int)
        self.quarantined = set()
        self.current = {}                                 # sensor -> latest verdict body (for the 20 s refresh)
        self.last_sent = time.time()
        self.lock = threading.Lock()

    def start(self):
        self.p.start(self._connected, self._message)
        while True:
            time.sleep(1)
            if time.time() - self.last_sent > self.c["refresh_s"]:
                with self.lock:
                    for sid, v in list(self.current.items()):
                        self._send(sid, v["verdict"], v["reason"], v["trust_score"], v["correlation_id"], "refresh")

    def _connected(self):
        for t in ("twin/meta", "city/+/sensor/+/+", "twin/actuator/+", "twin/unit/+"):
            self.p.client.subscribe(t, qos=1)
        self.p.client.publish("guardian/status", seal(self.p.body("status", status="online"), {"x": self.p.guardian_brain}), 1, True)
        print("[guardian-mock] connected", flush=True)

    def _send(self, sid, verdict, reason, score, corr, details="", retain=True):
        b = self.p.body("verdict", sensor_id=sid, verdict=verdict, reason=reason, trust_score=score, correlation_id=corr)
        if details:
            b["details"] = details
        self.p.client.publish("guardian/verdict/" + sid, seal(b, {"x": self.p.guardian_brain}), 1, retain)
        self.last_sent = time.time()
        self.current[sid] = b

    def _message(self, m):
        try:
            body = open_envelope(m.payload, self.p.twin_guardian, "guardian")
        except ProtocolError as e:
            print("[guardian-mock] %s: %s" % (m.topic, e), flush=True)
            return
        with self.lock:
            if m.topic == "twin/meta":
                self.meta = {s["sensor_id"]: s for s in body.get("sensors", [])}
                self.seq.clear()
                print("[guardian-mock] meta: %d sensors" % len(self.meta), flush=True)
            elif "/sensor/" in m.topic:
                self._reading(body)

    def _reading(self, r):
        sid, v, c = r["sensor_id"], r["value"], self.c
        decl = self.meta.get(sid)
        verdict, reason, score = "trusted", "consistent", 0.95
        if decl is None:
            return
        if r["id"] in self.ids and time.time() - self.ids[r["id"]] < 60:
            return self._send(sid, "rejected", "replay", 0.0, r["id"], retain=False)
        self.ids[r["id"]] = time.time()
        if not decl["min"] <= v <= decl["max"]:
            verdict, reason, score = "rejected", "out_of_range", 0.0
        elif self.seq.get(sid, -1) >= r.get("seq", math.inf):
            verdict, reason, score = "rejected", "replay", 0.0
        elif sid in self.last and abs(v - self.last[sid]) > c["max_jump_frac"] * (decl["max"] - decl["min"]):
            verdict, reason, score = "rejected", "implausible_jump", 0.05
        else:
            h = self.hist[sid]
            h.append(v)
            if len(h) == h.maxlen and len(set(h)) == 1:
                verdict, reason, score = "suspicious", "frozen", 0.4
        self.seq[sid] = max(self.seq.get(sid, -1), r.get("seq", -1))
        if verdict == "rejected":
            self.rejected_run[sid] += 1
            if self.rejected_run[sid] >= c["quarantine_after"] and sid not in self.quarantined:
                self.quarantined.add(sid)
                q = self.p.body("quarantine", device_id=sid, state="quarantined", reason=reason)
                self.p.client.publish("guardian/quarantine/" + sid, seal(q, {"twin": self.p.twin_guardian, "brain": self.p.guardian_brain}), 1, True)
        else:
            self.last[sid] = v
            self.rejected_run[sid] = 0
            if sid in self.quarantined and self.hist[sid].maxlen:
                self.quarantined.discard(sid)
                q = self.p.body("quarantine", device_id=sid, state="released", reason="consistent_again")
                self.p.client.publish("guardian/quarantine/" + sid, seal(q, {"twin": self.p.twin_guardian, "brain": self.p.guardian_brain}), 1, True)
        self._send(sid, verdict, reason, score, r["id"])


class Brain:
    """Flood playbook only: a trusted water_level at or above its alert level 3 times in a row -> open the valve of the zone."""

    def __init__(self, cfg):
        self.c, self.p = cfg["brain"], Peer(cfg, "brain", "brain/status")
        self.meta, self.over, self.trust, self.cmds = {}, defaultdict(int), {}, {}
        self.opened = {}

    def start(self):
        self.p.start(self._connected, self._message)
        while True:
            time.sleep(1)

    def _connected(self):
        for t in ("twin/meta", "city/+/sensor/water_level/+", "twin/actuator/+", "twin/unit/+", "guardian/verdict/+", "twin/status"):
            self.p.client.subscribe(t, qos=1)
        self.p.client.publish("brain/status", seal(self.p.body("status", status="online"), {"x": self.p.guardian_brain}), 1, True)
        print("[brain-mock] connected", flush=True)

    def _message(self, m):
        try:
            key = self.p.guardian_brain if m.topic.startswith("guardian/") else self.p.twin_brain
            body = open_envelope(m.payload, key, "brain")
        except ProtocolError as e:
            if not m.topic.startswith("guardian/"):
                print("[brain-mock] %s: %s" % (m.topic, e), flush=True)
            return
        if m.topic == "twin/meta":
            self.meta = body
        elif m.topic.startswith("guardian/verdict/"):
            self.trust[body["sensor_id"]] = body["verdict"]
        elif m.topic.startswith("twin/actuator/") and body.get("correlation_id") in self.cmds:
            print("[brain-mock] ack of %s: %s (%s)" % (body["correlation_id"], body.get("result"), body.get("state")), flush=True)
        elif "/sensor/water_level/" in m.topic:
            self._water(body)

    def _water(self, r):
        decl = next((s for s in self.meta.get("sensors", []) if s["sensor_id"] == r["sensor_id"]), None)
        if not decl or decl.get("alert") is None:
            return
        trusted = self.trust.get(r["sensor_id"]) == "trusted"
        self.over[r["sensor_id"]] = self.over[r["sensor_id"]] + 1 if (r["value"] >= decl["alert"] and trusted) else 0
        if self.over[r["sensor_id"]] >= self.c["confirm_readings"] and time.time() - self.opened.get(r["zone_id"], 0) > 30:
            valve = next((a for a in self.meta.get("actuators", []) if a["type"] == "valve" and r["zone_id"] in a["controls"]), None)
            if valve:
                self.opened[r["zone_id"]] = time.time()
                cmd = self.p.body("cmd", correlation_id=r["id"], target_id=valve["actuator_id"], action="open", parameters={"opening": 1.0})
                self.cmds[cmd["id"]] = cmd
                self.p.client.publish("brain/cmd/" + valve["actuator_id"],
                                      seal(cmd, {"twin": self.p.twin_brain, "guardian": self.p.guardian_brain}), 1)
                print("[brain-mock] flood at %s (%s %.2f m, trusted): open %s" % (r["zone_id"], r["sensor_id"], r["value"], valve["actuator_id"]), flush=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    cfg = load()
    if which in ("guardian", "both"):
        g = Guardian(cfg)
        threading.Thread(target=g.start, daemon=True).start()
    if which in ("brain", "both"):
        b = Brain(cfg)
        threading.Thread(target=b.start, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
