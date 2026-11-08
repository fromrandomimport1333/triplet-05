"""City Brain protocol profile, part 3: the MQTT/TLS bridge between the Twin and the real Brain and Guardian.

The Twin core is unchanged: it still produces its native messages and still takes commands from a queue. The bridge
  * connects to the partners' broker (TLS, account, CA), publishes `twin/meta` first (retained) then `twin/status`
    (retained, Last Will `offline`);
  * translates every native reading / call / feedback into the partners' bodies and seals them (hmacs for Brain and
    Guardian);
  * listens to `brain/cmd/+` (verifies hmacs.twin, freshness, applies each id at most once), turns commands into the
    Twin's native commands and acknowledges on `twin/actuator/<id>` / `twin/unit/<id>`;
  * listens to `guardian/quarantine/+` and applies it; logs `brain/error`.
All settings come from `integration:` in city.yaml (environment variables override); nothing is hard-coded.
"""
import json
import os
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from protocol import ProtocolError, check_fresh, open_envelope, read_key, require_fields, seal
from translate import CityMap, Reject


def log(msg):
    print("[bridge] " + msg, flush=True)


def _path(cfg_dir, p):
    return p if not p or os.path.isabs(p) else os.path.normpath(os.path.join(cfg_dir, p))


class Bridge:
    def __init__(self, twin, icfg, cfg_dir):
        self.twin, self.icfg = twin, icfg
        run_id = icfg.get("run_id") or "tw" + _base36(int(time.time()))
        self.map = CityMap(icfg, twin.devices, twin.cfg.get("dispatch", {}), run_id)
        m = icfg["mqtt"]
        self.keys = {}
        for layer in ("brain", "guardian"):
            f = _path(cfg_dir, (icfg.get("keys") or {}).get(layer))
            if f and os.path.exists(f):
                self.keys[layer] = read_key(f)
            else:
                log("WARNING: no %s key file (%s): messages are not sealed for %s" % (layer, f, layer))
        if "brain" not in self.keys:
            raise SystemExit("integration: the Brain key file is required (integration.keys.brain)")
        self.window = float(icfg.get("clock_window_s", 10))
        self.qos_readings = int(icfg.get("qos_readings", 0))
        self.lock = threading.RLock()
        self.pending = {}        # native command id -> info of a Brain command awaiting feedback
        self.finished = {}       # Brain command id -> (topic, sealed ack) : a retry gets the same answer
        self.seen_ids = {}       # Brain command id -> time first seen
        self.unit_state = {}     # exposed unit id -> {"status", "zone"}
        self.quarantined = set()  # exposed ids of actuators / units isolated by the Guardian
        self.ready = threading.Event()
        self.stats = {"sent": 0, "commands": 0, "refused": 0, "errors_from_brain": 0}
        cid = m.get("client_id") or "twin-" + run_id
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:
            self.client = mqtt.Client(client_id=cid)
        if m.get("tls", True):
            ca = _path(cfg_dir, m.get("ca_cert"))
            self.client.tls_set(ca_certs=ca, cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        pw_file = _path(cfg_dir, m.get("password_file"))
        password = open(pw_file, encoding="utf-8").read().strip() if pw_file and os.path.exists(pw_file) else m.get("password")
        if m.get("username"):
            self.client.username_pw_set(m["username"], password)
        self.client.reconnect_delay_set(1, 30)
        # Last Will: if the Twin dies, the broker tells everybody. Retained, sealed for both partners.
        _, will = self.map.status("offline")
        self.client.will_set("twin/status", self._seal(will), qos=1, retain=True)
        self.client.on_connect, self.client.on_message, self.client.on_disconnect = self._on_connect, self._on_message, self._on_disconnect
        self.host, self.port = m["host"], int(m["port"])

    # ---------- lifecycle ----------
    def start(self):
        self.client.connect_async(self.host, self.port, int(self.icfg["mqtt"].get("keepalive_s", 30)))
        self.client.loop_start()
        log("connecting to %s:%s (TLS=%s)" % (self.host, self.port, self.icfg["mqtt"].get("tls", True)))

    def stop(self):
        try:
            _, body = self.map.status("offline")
            self._publish("twin/status", body, qos=1, retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    def _on_connect(self, c, userdata, flags, rc, props=None):
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            log("CONNECT REFUSED: %s (check account, password file, CA)" % rc)
            return
        log("connected")
        for topic in ("brain/cmd/+", "brain/error", "guardian/quarantine/+"):
            c.subscribe(topic, qos=1)
        # The Brain learns the whole city from this message: first, retained.
        self._publish("twin/meta", self.map.meta(), qos=1, retain=True)
        self._publish("twin/status", self.map.status("online")[1], qos=1, retain=True)
        for uid, u in self.map.units.items():
            self.unit_state[uid] = {"status": "free", "zone": u["home_zone"]}
            self._publish(*self.map.unit_message(uid, status="free", zone=u["home_zone"]), qos=1, retain=False)
        self.ready.set()

    def _on_disconnect(self, c, userdata, *args):
        self.ready.clear()
        log("disconnected, will retry")

    # ---------- sending ----------
    def _seal(self, body):
        return seal(body, self.keys)

    def _publish(self, topic, body, qos=1, retain=False):
        info = self.client.publish(topic, self._seal(body), qos=qos, retain=retain)
        self.stats["sent"] += 1
        return info

    def on_native(self, topic, msg):
        """Called by Twin.publish for every native message. Only the partners' contract leaves the Twin."""
        if not self.ready.is_set():
            return
        kind, typ = msg.get("kind"), msg.get("type")
        try:
            if typ == "reading" and kind == "emergency_call":
                t, b = self.map.call(msg)
                self._publish(t, b, qos=1)
            elif typ == "reading":
                for t, b in self.map.readings(msg):
                    self._publish(t, b, qos=self.qos_readings)
            elif typ == "feedback":
                self._feedback(msg)
            # ack, fault_event and anything else stay inside the Twin (ground truth never goes on the bus)
        except Exception as e:  # a translation bug must not stop the simulation
            log("translation error on %s: %s" % (topic, e))

    def _feedback(self, fb):
        with self.lock:
            info = self.pending.get(fb["cmd_id"])
            native = fb["actuator_id"]
            if info is None:                       # not a Brain command (scenario reset, REST test command): plain state
                if native in self.map.actuator_native:
                    self._publish(*self.map.actuator_state(native, fb.get("state")), qos=1)
                return
            if info["kind"] == "actuator":
                result = "done" if fb.get("ok") else "failed"
                t, b = self.map.actuator_state(native, fb.get("state"), fb["cmd_id"], result, None if fb.get("ok") else fb.get("detail"))
                self._finish(fb["cmd_id"], t, b)
                return
            uid = info["target"]
            if fb.get("state") == "dispatched":
                self.unit_state[uid] = {"status": "en_route", "zone": info["zone"]}
                t, b = self.map.unit_message(uid, fb["cmd_id"], "done", "en_route", info["zone"], "dispatched")
                self._finish(fb["cmd_id"], t, b, keep=True)
            elif fb.get("state") == "recalled":
                home = self.map.units[uid]["home_zone"]
                self.unit_state[uid] = {"status": "free", "zone": home}
                t, b = self.map.unit_message(uid, fb["cmd_id"], "done", "returning", home, "recalled")
                self._finish(fb["cmd_id"], t, b)
                self._publish(*self.map.unit_message(uid, status="free", zone=home), qos=1)
            elif fb.get("state") == "arrived":
                self.pending.pop(fb["cmd_id"], None)
                self.unit_state[uid] = {"status": "on_scene", "zone": info["zone"]}
                self._publish(*self.map.unit_message(uid, status="on_scene", zone=info["zone"]), qos=1)
            else:                                   # no_unit or failure
                t, b = self.map.unit_message(uid, fb["cmd_id"], "failed", reason=fb.get("detail") or fb.get("state"))
                self._finish(fb["cmd_id"], t, b)

    def _finish(self, cmd_id, topic, body, keep=False):
        sealed = self._seal(body)
        self.finished[cmd_id] = (topic, sealed)
        self.client.publish(topic, sealed, qos=1)
        self.stats["sent"] += 1
        if not keep:
            self.pending.pop(cmd_id, None)

    def tick(self):
        """Called once per simulation step: units that finished their time on scene become free again."""
        with self.lock:
            for uid, st in self.unit_state.items():
                if st["status"] == "on_scene":
                    busy = self.twin.units[self.map.units[uid]["native"]]["busy_until"]
                    if busy is None or busy <= self.twin.t:
                        home = self.map.units[uid]["home_zone"]
                        self.unit_state[uid] = {"status": "free", "zone": home}
                        self._publish(*self.map.unit_message(uid, status="returning", zone=home), qos=1)
                        self._publish(*self.map.unit_message(uid, status="free", zone=home), qos=1)

    # ---------- receiving ----------
    def _on_message(self, c, userdata, m):
        try:
            if m.topic.startswith("brain/cmd/"):
                self._on_command(m.topic, m.payload)
            elif m.topic == "brain/error":
                self.stats["errors_from_brain"] += 1
                body = self._open(m.payload, "brain")
                log("BRAIN REFUSED one of our messages: %s %s (message %s, topic %s)" % (
                    body.get("code"), body.get("detail"), body.get("message_id"), body.get("topic")))
            elif m.topic.startswith("guardian/quarantine/"):
                self._on_quarantine(m.topic, m.payload)
        except ProtocolError as e:
            self.stats["refused"] += 1
            log("refused %s: %s %s" % (m.topic, e.code, e.detail))
        except Exception as e:
            self.stats["refused"] += 1
            log("error handling %s: %s" % (m.topic, e))

    def _open(self, payload, layer):
        key = self.keys.get(layer)
        if key is None:
            raise ProtocolError("E_AUTH", "no key configured for %s" % layer)
        return open_envelope(payload, key, "twin")

    def _ack_reject(self, kind, target, cmd_id, reason):
        if kind == "unit":
            u = self.map.units[target]
            t, b = self.map.unit_message(target, cmd_id, "rejected", reason=reason)
        else:
            t, b = self.map.actuator_state(self.map.actuators[target]["native"], self.twin.actuator_state.get(self.map.actuators[target]["native"]),
                                           cmd_id, "rejected", reason)
        self._finish(cmd_id, t, b)

    def _on_command(self, topic, payload):
        body = self._open(payload, "brain")
        check_fresh(body, time.time(), self.window)
        require_fields(body, {"id": str, "target_id": str, "action": str})
        if body.get("source") != "brain":
            raise ProtocolError("E_FORMAT", "source is %r on a brain topic" % body.get("source"))
        if topic.rsplit("/", 1)[1] != body["target_id"]:
            raise ProtocolError("E_FORMAT", "topic says %s but body says target_id=%s" % (topic.rsplit("/", 1)[1], body["target_id"]))
        if not isinstance(body.get("parameters", {}), dict):
            raise ProtocolError("E_FORMAT", "parameters: expected object")
        cmd_id = body["id"]
        with self.lock:
            if cmd_id in self.seen_ids:                        # the Brain's retry: apply once, answer again
                if cmd_id in self.finished:
                    self.client.publish(*self.finished[cmd_id], qos=1)
                log("command %s repeated: not applied twice" % cmd_id)
                return
            self.seen_ids[cmd_id] = time.time()
            self.stats["commands"] += 1
            try:
                aid, args, (kind, target) = self.map.native_command(body)
            except Reject as e:
                where = self.map.device_native(body["target_id"])
                log("command %s rejected: %s" % (cmd_id, e))
                self._ack_reject(where[0], body["target_id"], cmd_id, str(e))
                return
            if target in self.quarantined:
                log("command %s rejected: %s is quarantined" % (cmd_id, target))
                self._ack_reject(kind, target, cmd_id, "quarantined")
                return
            err = None if args.get("recall") else self._command_error(aid, args)
            if err:
                log("command %s rejected: %s" % (cmd_id, err))
                self._ack_reject(kind, target, cmd_id, err)
                return
            info = {"kind": kind, "target": target}
            if kind == "unit":
                info["zone"] = args.get("zone_id")
            self.pending[cmd_id] = info
            if args.get("recall"):
                self.twin.cmds.put(("__recall__", cmd_id, args["unit_id"]))
            else:
                self.twin.cmds.put((aid, cmd_id, args, body.get("correlation_id")))
            log("command %s: %s %s -> %s %s" % (cmd_id, body["action"], body["target_id"], aid, json.dumps(args)))

    def _command_error(self, aid, args):
        return self.twin.command_problem(aid, args)

    def _on_quarantine(self, topic, payload):
        body = self._open(payload, "guardian")      # key: twin-guardian.key; accepts hmacs.twin or hmac
        require_fields(body, {"device_id": str, "state": str})
        if body["state"] not in ("quarantined", "released"):
            raise ProtocolError("E_FORMAT", "state: %r is not quarantined/released" % body["state"])
        if topic.rsplit("/", 1)[1] != body["device_id"]:
            raise ProtocolError("E_FORMAT", "topic and body device_id disagree")
        where = self.map.device_native(body["device_id"])
        if where is None:
            raise ProtocolError("E_UNKNOWN_ID", "device %s is not in this city" % body["device_id"])
        on = body["state"] == "quarantined"
        kind, native = where
        with self.lock:
            if kind == "sensor":
                (self.twin.quarantined.add if on else self.twin.quarantined.discard)(native)
            else:
                (self.quarantined.add if on else self.quarantined.discard)(body["device_id"])
        log("%s %s (%s)" % (body["device_id"], body["state"], body.get("reason")))


def _base36(n):
    digits, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"
