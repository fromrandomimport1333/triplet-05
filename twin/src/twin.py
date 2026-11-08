"""TSYP Twin: SUMO bridge + REST API + MQTT publisher in one process.

Run:   python twin/src/twin.py                 (SUMO scenario from twin/city.yaml)
       python twin/src/twin.py --fake          (no SUMO, traffic values are simulated; for testing)
       python twin/src/twin.py --config other.yaml --speed 5

Data flow
  SUMO / Python models --> signed JSON readings --> MQTT  city/<zone>/<kind>/<id>
  Brain --> REST POST /actuators/<id> or /tools/<tool> --> ack --> applied inside the simulation loop
        --> feedback on MQTT city/<zone>/actuator/<id>/state
  Guardian --> REST POST /quarantine/<sensor_id> (Guardian key only) --> readings carry "quarantined": true
  Test scripts --> REST POST /faults, /scenario/... --> faults and conditions (ground truth kept, see /truth)

TraCI is not thread-safe, so the REST thread never touches SUMO: it puts commands in a queue and
the main loop applies them between simulation steps.
"""
import argparse
import itertools
import json
import math
import os
import queue
import random
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from typing import Literal

import uvicorn
import yaml
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from contract import CONTRACT, Schemas, apply_env, apply_integration_env, error_body, now_iso, sign
from jsonlist import JsonListFile

try:
    import paho.mqtt.client as mqtt
except ImportError:  # MQTT is optional so the REST side can be tested alone
    mqtt = None

UNITS = {"traffic": "km/h", "traffic_camera": "vehicles", "water_level": "m", "rain": "mm/h", "air_quality": "ug/m3",
         "power_load": "%", "emergency_call": "call"}
COLORS = {"traffic": (255, 140, 0, 255), "traffic_camera": (255, 80, 0, 255), "water_level": (0, 120, 255, 255),
          "rain": (120, 200, 255, 255), "emergency_call": (255, 0, 80, 255),
          "air_quality": (150, 150, 150, 255), "power_load": (255, 220, 0, 255),
          "traffic_signal": (0, 200, 0, 255), "road_closure": (220, 0, 0, 255),
          "water_valve": (0, 60, 160, 255), "public_alert": (200, 0, 200, 255),
          "power_grid": (255, 255, 0, 255), "emergency_dispatch": (255, 255, 255, 255),
          "device_quarantine": (60, 60, 60, 255)}

FAKE_SPEED_LIMIT_KMH = 50.0   # --fake mode has no roads, so it uses a typical city limit
FAKE_CAMERA_CAPACITY = 30     # vehicles a camera's road holds when full, in --fake mode
DEFAULT_SIGNAL_PHASES = 4     # for a signal device with no SUMO signal, such as in --fake mode
STATE_VALUES = {"road_closure": ("closed", "open"), "water_valve": ("open", "closed")}
ALERT_LEVELS = ("info", "yellow", "orange", "red")
ALERT_MAX_CHARS = 280
GRID_ACTIONS = {"normal": 1.0, "reroute": 0.8, "shed": 0.65}   # factor applied to that zone's power-load readings
QUARANTINE_STATES = ("quarantined", "released")
SENSOR_FAULTS = ("stuck", "dropout", "spoof", "spike", "drift", "replay", "flood_network")
CAMERA_FIELDS = ("mean_speed_kmh", "halted_vehicles", "density_pct")
REPLAY_BUFFER = 30            # genuine messages kept per sensor, for the replay fault
MAX_FLOOD_RATE = 200          # extra messages per publish, at most, for the flood_network fault

# Emergency call type -> (number dialled, service, share of simulated calls)
CALL_TYPES = {"medical": (190, "SAMU", 35), "accident": (190, "SAMU", 25), "crime": (197, "police", 20),
              "fire": (198, "civil_protection", 12), "flood": (198, "civil_protection", 8)}

# US EPA PM2.5 breakpoints (2024): (concentration low, high, AQI low, high)
AQI_PM25 = [(0.0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150), (55.5, 125.4, 151, 200),
            (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500)]

# Tool names from the Phase 0 contract -> actuator type, and how tool args become actuator args.
TOOLS = {
    "set_water_valve": {"type": "water_valve", "args": ["zone_id", "state"]},
    "set_traffic_signal": {"type": "traffic_signal", "args": ["zone_id", "phase"]},
    "set_road_closure": {"type": "road_closure", "args": ["zone_id", "state"]},
    "set_power_grid": {"type": "power_grid", "args": ["zone_id", "action"]},
    "broadcast_alert": {"type": "public_alert", "args": ["zone_id", "level", "text"]},
    "dispatch_emergency": {"type": "emergency_dispatch", "args": ["type", "zone_id"]},
    "quarantine_device": {"type": "device_quarantine", "args": ["sensor_id"]},
    "release_device": {"type": "device_quarantine", "args": ["sensor_id"]},
}


def in_bbox(lonlat, bbox):
    west, south, east, north = bbox
    return west <= lonlat[0] <= east and south <= lonlat[1] <= north


def pm25_to_aqi(c):
    if c is None:
        return None
    c = max(0.0, float(c))
    for lo, hi, alo, ahi in AQI_PM25:
        if c <= hi + 0.05:
            return int(round(alo + (ahi - alo) * (min(max(c, lo), hi) - lo) / (hi - lo)))
    return 500


def camera_from_count(count, capacity, limit):
    """Speed, stopped vehicles and density that are consistent with a vehicle count.

    Used for every faked camera reading (spoof, spike, drift) and for all --fake readings, so a fake count never
    contradicts the speed and stopped-vehicle fields next to it."""
    density = min(100.0, 100.0 * max(0, count) / max(1.0, capacity))
    speed = max(1.0, limit * (1 - 0.95 * (density / 100.0) ** 2))
    halted = int(round(max(0, count) * max(0.0, (density - 60) / 40))) if density > 60 else 0
    return {"mean_speed_kmh": round(min(speed, limit), 1), "halted_vehicles": halted, "density_pct": round(density, 1)}


def haversine_m(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def command_error(aid, actuator_type, args, phases, ctx=None):
    """Why these command args are invalid for this actuator, or None if they are fine.

    A phase SUMO does not have would raise in the main loop and stop the simulation, so bad args must never be queued.
    ctx (optional) gives what some checks need: {"zones": [...], "unit_types": [...], "sensors": [...]}.
    """
    ctx = ctx or {}
    if not isinstance(args, dict):
        return "%s: args must be an object" % aid
    if actuator_type == "traffic_signal":
        wanted = "args.phase must be a whole number from 0 to %d" % (phases - 1)
        if "phase" not in args:
            return "%s: %s, but it is missing" % (aid, wanted)
        phase = args["phase"]
        if type(phase) is not int or not 0 <= phase < phases:     # type(): JSON true is a bool, and bool is an int
            return "%s: %s, got %s" % (aid, wanted, json.dumps(phase))
    elif actuator_type in STATE_VALUES:
        wanted = "args.state must be %s" % " or ".join('"%s"' % s for s in STATE_VALUES[actuator_type])
        if "state" not in args:
            return "%s: %s, but it is missing" % (aid, wanted)
        if args["state"] not in STATE_VALUES[actuator_type]:
            return "%s: %s, got %s" % (aid, wanted, json.dumps(args["state"]))
    elif actuator_type == "public_alert":
        if args.get("level") not in ALERT_LEVELS:
            return "%s: args.level must be one of %s, got %s" % (aid, ", ".join(ALERT_LEVELS), json.dumps(args.get("level")))
        text = args.get("msg", args.get("text"))
        if not isinstance(text, str) or not text.strip():
            return "%s: args.msg must be a non-empty text" % aid
        if len(text) > ALERT_MAX_CHARS:
            return "%s: args.msg must be at most %d characters, got %d" % (aid, ALERT_MAX_CHARS, len(text))
    elif actuator_type == "power_grid":
        if args.get("action") not in GRID_ACTIONS:
            return "%s: args.action must be one of %s, got %s" % (aid, ", ".join(GRID_ACTIONS), json.dumps(args.get("action")))
    elif actuator_type == "emergency_dispatch":
        types = ctx.get("unit_types") or ["police", "ambulance", "civil_protection"]
        if args.get("type") not in types:
            return "%s: args.type must be one of %s, got %s" % (aid, ", ".join(types), json.dumps(args.get("type")))
        if args.get("unit_id") is not None and not isinstance(args["unit_id"], str):
            return "%s: args.unit_id must be a text (optional: a specific unit), got %s" % (aid, json.dumps(args["unit_id"]))
        zones = ctx.get("zones")
        if not isinstance(args.get("zone_id"), str) or (zones and args["zone_id"] not in zones):
            return "%s: args.zone_id must be one of %s, got %s" % (aid, ", ".join(zones or []), json.dumps(args.get("zone_id")))
    elif actuator_type == "device_quarantine":
        sensors = ctx.get("sensors")
        if not isinstance(args.get("sensor_id"), str) or (sensors is not None and args["sensor_id"] not in sensors):
            return "%s: args.sensor_id must be a known sensor id, got %s" % (aid, json.dumps(args.get("sensor_id")))
        if args.get("state", "quarantined") not in QUARANTINE_STATES:
            return "%s: args.state must be quarantined or released, got %s" % (aid, json.dumps(args.get("state")))
    return None


class Twin:
    def __init__(self, cfg, cfg_dir, fake, speed):
        self.cfg, self.cfg_dir, self.fake, self.speed = cfg, cfg_dir, fake, speed
        self.devices = {d["id"]: d for d in cfg["devices"]}
        self.cmds = queue.Queue()
        self.seq = {}
        self.faults = {}            # sensor_id -> fault dict
        self.fault_lock = threading.RLock()   # faults start in the REST thread and end in the main loop
        self.truth = {}             # sensor_id -> true value (never published)
        self.truth_fields = {}      # camera id -> true speed / stopped / density
        self.last = {}              # sensor_id -> last published message
        self.history = {}           # sensor_id -> recent genuine messages (for the replay fault)
        self.actuator_state = {d: "idle" for d, v in self.devices.items() if v["class"] == "actuator"}
        self.valve_open = {}
        self.quarantined = set()    # sensor ids the Guardian has quarantined
        self.zones = sorted({d["zone"] for d in self.devices.values() if d["zone"] != "city"})
        self.rain_mm_h = 0.0        # city-wide rain; a zone can override it in self.conditions
        self.conditions = {z: self.default_conditions() for z in self.zones}
        self.conditions_dirty = False
        self.grid_action = {z: "normal" for z in self.zones}
        for aid, d in self.devices.items():
            if d["type"] == "power_grid":
                self.actuator_state[aid] = "normal"
        disp = cfg.get("dispatch", {})
        self.units = {u["id"]: dict(u, busy_until=None) for u in disp.get("units", [])}
        self.arrivals = []          # dispatched units still on their way
        self.t = 0.0
        self.running = True
        self.traci = None
        self.net = None
        self.edges = {}             # device id -> SUMO edge id
        self.speed_limits = {}      # device id -> speed limit of its edge, in km/h
        self.capacity = {}          # camera id -> vehicles its edge holds when full
        self.tls = {}               # device id -> traffic light id
        self.signal_phases = {}     # device id -> number of phases its traffic light has
        self.next_due = {}          # sensor id -> sim time of its next reading
        self.bridge = None          # City Brain protocol bridge (bridge.py), set by main() when integration.enabled
        self.mqtt = self._connect_mqtt()
        logs = cfg.get("logs", {})
        logs_dir = os.path.join(cfg_dir, logs.get("dir", "logs"))
        os.makedirs(logs_dir, exist_ok=True)
        self.log = JsonListFile(os.path.join(logs_dir, "twin_events.json"),
                                int(logs.get("rotate_mb", 20) * 1000000), logs.get("keep_files", 5))
        # These two are small and are not rotated, so they outlive the rotated readings.
        self.calls = JsonListFile(os.path.join(logs_dir, "emergency_calls.json"))
        self.fault_log = JsonListFile(os.path.join(logs_dir, "fault_events.json"))
        self.call_numbers = itertools.count(self.calls.count + 1)
        self.call_rng = random.Random(43)   # separate, so calls do not change the other sensors' values
        self.call_edges = {}                # emergency sensor id -> roads where simulated calls can happen
        self.water = {i: 0.4 for i, d in self.devices.items() if d["type"] == "water_level"}
        self.rng = random.Random(42)   # deterministic: scenarios are reproducible
        self.fake_jam = {}             # camera id -> smoothed jam level actually reached (traffic builds up gradually)
        self.schemas = Schemas(os.path.normpath(os.path.join(cfg_dir, cfg.get("schemas_dir", "../docs/schemas"))))

    @staticmethod
    def default_conditions():
        return {"rain_mm_h": None, "jam": 0.0, "pollution": 1.0, "power_demand": 1.0}

    # ---------- MQTT / logging ----------
    def _connect_mqtt(self):
        if mqtt is None:
            print("[mqtt] paho-mqtt not installed, publishing disabled")
            return None
        if self.cfg["mqtt"].get("enabled", True) is False:
            return None
        if self.cfg.get("integration", {}).get("enabled") and self.cfg["integration"].get("disable_native_mqtt", True):
            print("[mqtt] integration profile on: the native broker is not used (the bridge talks to the partners' broker)")
            return None
        try:
            try:
                c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except AttributeError:
                c = mqtt.Client()
            m = self.cfg["mqtt"]
            if m.get("username"):
                c.username_pw_set(m["username"], m.get("password", ""))
            c.connect(m["host"], int(m["port"]), 30)
            c.loop_start()
            print("[mqtt] connected to %s:%s" % (self.cfg["mqtt"]["host"], self.cfg["mqtt"]["port"]))
            return c
        except OSError as e:
            print("[mqtt] broker not reachable (%s). Start mosquitto; continuing without MQTT." % e)
            return None

    def publish(self, topic, msg):
        payload = json.dumps(msg, separators=(",", ":"))
        if self.mqtt:
            self.mqtt.publish(topic, payload, qos=1)
        self.log.append({"ts": msg.get("ts", now_iso()), "topic": topic, "payload": msg})
        if self.bridge:
            self.bridge.on_native(topic, msg)

    def command_problem(self, aid, args):
        """Why these args are invalid for this actuator (None if fine). Used by the REST API and the bridge."""
        return command_error(aid, self.devices[aid]["type"], args, self.signal_phases.get(aid, DEFAULT_SIGNAL_PHASES),
                             self.validation_ctx())

    def sign(self, msg):
        return sign(msg, self.cfg["api"]["hmac_key"])

    # ---------- SUMO ----------
    def start_sumo(self):
        import sumolib
        import traci
        self.traci = traci
        cfg_path = os.path.normpath(os.path.join(self.cfg_dir, self.cfg["sumo"]["cfg"]))
        net_file = ET.parse(cfg_path).getroot().find(".//net-file").get("value")
        self.net = sumolib.net.readNet(os.path.join(os.path.dirname(cfg_path), net_file))
        binary = sumolib.checkBinary("sumo-gui" if self.cfg["sumo"]["gui"] else "sumo")
        traci.start([binary, "-c", cfg_path, "--step-length", str(self.cfg["sumo"]["step_length"]),
                     "--start", "--quit-on-end"])
        for d in self.devices.values():
            x, y = self.net.convertLonLat2XY(*d["lonlat"])
            d["xy"] = (x, y)
            if d["source"] == "sumo" or d["type"] in ("traffic_signal", "road_closure"):
                edge = self.pick_edge(d, x, y)
                if edge:
                    self.edges[d["id"]] = edge
                    if d["type"] in ("traffic", "traffic_camera"):
                        self.speed_limits[d["id"]] = self.edge_speed_limit(edge)
                        e = self.net.getEdge(edge)
                        self.capacity[d["id"]] = d.get("capacity") or max(1.0, e.getLength() * e.getLaneNumber() / 7.5)
                        print("[road] %s: edge %s, limit %.1f km/h" % (d["id"], edge, self.speed_limits[d["id"]]))
            if d["type"] == "traffic_signal":
                ids = traci.trafficlight.getIDList()
                if ids:
                    self.tls[d["id"]] = min(ids, key=lambda i: self._tls_dist(i, x, y))
                    self.signal_phases[d["id"]] = len(traci.trafficlight.getAllProgramLogics(self.tls[d["id"]])[0].phases)
            traci.poi.add(d["id"], x, y, COLORS.get(d["type"], (255, 255, 255, 255)),
                          poiType=d["type"], layer=20)

    def pick_edge(self, d, x, y):
        """The edge named in the config, otherwise the nearest one, with a warning if that match looks unsafe."""
        if d.get("edge"):
            if self.net.hasEdge(d["edge"]):
                return d["edge"]
            print("[warn] %s: edge %s is not on this map, so this device is not attached" % (d["id"], d["edge"]))
            return None
        near = sorted(self.net.getNeighboringEdges(x, y, 200), key=lambda e: e[1])
        if not near:
            print("[warn] %s: no street within 200 m of %s, so this device is not attached" % (d["id"], d["lonlat"]))
            return None
        best, dist = near[0]
        if d["type"] == "traffic_signal":    # it is controlled through its signal, so the road does not matter
            return best.getID()
        if dist > 50:
            print("[warn] %s: nearest street %s is %.0f m away; set edge: in the config" % (d["id"], best.getID(), dist))
        for other, other_dist in near[1:]:
            if other_dist - dist < 5 and other.getSpeed() != best.getSpeed():
                print("[warn] %s: streets %s and %s are almost equally near but have different limits; set edge: in the config"
                      % (d["id"], best.getID(), other.getID()))
                break
        return best.getID()

    def edge_speed_limit(self, edge):
        lanes = self.traci.edge.getLaneNumber(edge)
        return max(self.traci.lane.getMaxSpeed("%s_%d" % (edge, i)) for i in range(lanes)) * 3.6

    def road_speed(self, d):
        """Mean speed on the device's edge in km/h, capped at the limit. An empty edge reports the limit."""
        edge = self.edges[d["id"]]
        limit = self.speed_limits[d["id"]]
        if self.traci.edge.getLastStepVehicleNumber(edge) == 0:
            return round(limit, 1)
        # SUMO gives each vehicle its own speed factor, so its mean speed can pass the limit.
        return round(min(self.traci.edge.getLastStepMeanSpeed(edge) * 3.6, limit), 1)

    def _tls_dist(self, tls_id, x, y):
        try:
            node = self.net.getNode(tls_id)
            nx, ny = node.getCoord()
            return math.hypot(nx - x, ny - y)
        except Exception:
            return 1e9

    def apply_jam_sumo(self):
        """A jam condition lowers the speed limit on the camera roads of that zone (main loop only: uses TraCI)."""
        for sid, edge in self.edges.items():
            d = self.devices[sid]
            if d["type"] != "traffic_camera":
                continue
            jam = self.conditions.get(d["zone"], {}).get("jam", 0.0)
            limit_ms = self.speed_limits[sid] / 3.6
            self.traci.edge.setMaxSpeed(edge, max(0.5, limit_ms * (1 - 0.95 * jam)))

    # ---------- sensor models ----------
    def cond(self, zone, key):
        return self.conditions.get(zone, self.default_conditions())[key]

    def rain_for(self, zone):
        r = self.cond(zone, "rain_mm_h")
        return self.rain_mm_h if r is None else r

    def read_truth(self, d):
        t, typ, zone = self.t, d["type"], d["zone"]
        if typ == "traffic":
            if self.traci:     # never invent a value: a device with no road publishes nothing
                return self.road_speed(d) if d["id"] in self.edges else None
            jam = self.cond(zone, "jam")
            return round(min(FAKE_SPEED_LIMIT_KMH, max(2.0, (45 + 8 * math.sin(t / 60)) * (1 - 0.9 * jam) + self.rng.gauss(0, 2))), 1)
        if typ == "traffic_camera":
            if self.traci:
                if d["id"] not in self.edges:
                    return None
                edge = self.edges[d["id"]]
                count = self.traci.edge.getLastStepVehicleNumber(edge)
                cap = self.capacity.get(d["id"], FAKE_CAMERA_CAPACITY)
                self.truth_fields[d["id"]] = {"mean_speed_kmh": self.road_speed(d),
                                              "halted_vehicles": self.traci.edge.getLastStepHaltingNumber(edge),
                                              "density_pct": round(min(100.0, 100.0 * count / cap), 1)}
                return count
            cap = d.get("capacity", FAKE_CAMERA_CAPACITY)
            # traffic builds up and clears over about a minute rather than instantly
            target = self.cond(zone, "jam")
            jam = self.fake_jam.get(d["id"], 0.0)
            jam += (target - jam) * 0.05
            self.fake_jam[d["id"]] = jam
            base = 6 + 4 * math.sin(t / 60) + self.rng.gauss(0, 1)
            count = max(0, int(round(base * (1 - jam) + jam * cap * 0.95 + self.rng.gauss(0, 0.5) * jam)))
            fields = camera_from_count(count, cap, FAKE_SPEED_LIMIT_KMH)
            fields["mean_speed_kmh"] = round(max(1.0, min(FAKE_SPEED_LIMIT_KMH, fields["mean_speed_kmh"] + self.rng.gauss(0, 1))), 1)
            self.truth_fields[d["id"]] = fields
            return count
        if typ == "rain":
            rain = self.rain_for(zone)
            return round(rain + max(0, self.rng.gauss(0, 0.2)) * (rain > 0), 2)
        if typ == "water_level":
            i = d["id"]
            drain = 0.05 if self.valve_open.get(d.get("valve")) else 0.0
            self.water[i] = min(6.0, max(0.1, self.water[i] + 0.0004 * self.rain_for(zone) - drain + self.rng.gauss(0, 0.002)))
            return round(self.water[i], 3)
        if typ == "air_quality":
            return round(max(1.0, (22 + 4 * math.sin(t / 300) + self.rng.gauss(0, 1)) * self.cond(zone, "pollution")), 1)
        if typ == "power_load":
            base = (55 + 20 * math.sin(t / 900) + self.rng.gauss(0, 1)) * self.cond(zone, "power_demand")
            return round(min(110.0, max(0.0, base * GRID_ACTIONS[self.grid_action.get(zone, "normal")])), 1)
        return 0.0

    def camera_limit(self, sid):
        return self.speed_limits.get(sid, FAKE_SPEED_LIMIT_KMH)

    def camera_capacity(self, sid):
        return self.capacity.get(sid, self.devices[sid].get("capacity", FAKE_CAMERA_CAPACITY))

    # ---------- faults ----------
    def publish_fault_event(self, event, f, end_sim=None, end_utc=None, reason=None):
        msg = {"contract_version": CONTRACT, "type": "fault_event", "event": event, "fault_id": f["id"],
               "sensor_id": f["sensor_id"], "zone_id": self.devices[f["sensor_id"]]["zone"], "kind": f["kind"],
               "value": f["value"], "start_sim_time": f["start_sim"], "start_utc": f["start_utc"],
               "end_sim_time": end_sim, "end_utc": end_utc, "planned_end_sim_time": f["until"], "reason": reason,
               "ts": now_iso()}
        msg["sig"] = self.sign(msg)
        self.fault_log.append(msg)
        self.publish("city/faults", msg)

    def start_fault(self, sid, kind, value, duration_s):
        with self.fault_lock:
            self.end_fault(sid, "replaced")
            f = {"id": "F-" + uuid.uuid4().hex[:12], "sensor_id": sid, "kind": kind, "value": value, "start_sim": self.t,
                 "start_utc": now_iso(), "until": self.t + duration_s if duration_s else None}
            if kind == "replay":
                f["replay"], f["replay_i"] = list(self.history.get(sid, [])), 0
            self.faults[sid] = f
            self.publish_fault_event("start", f)
            return f

    def end_fault(self, sid, reason):
        with self.fault_lock:
            f = self.faults.pop(sid, None)
            if f:
                self.publish_fault_event("stop", f, self.t, now_iso(), reason)
            return f

    def expire_faults(self):
        with self.fault_lock:
            for sid, f in list(self.faults.items()):
                if f["until"] is not None and self.t > f["until"]:
                    self.end_fault(sid, "expired")

    def apply_fault(self, sid, truth):
        """Returns (value to publish, whether to publish it, label for a value the fault changed)."""
        with self.fault_lock:
            f = self.faults.get(sid)
            if not f:
                return truth, True, None
            k = f["kind"]
            if k in ("dropout", "replay"):        # replay: the genuine reading is replaced by an old one (see publish_sensors)
                return None, False, None
            label = {"kind": k, "id": f["id"]}
            if k == "stuck":
                f.setdefault("held", self.last.get(sid, {}).get("value", truth))
                return f["held"], True, label
            if k == "drift":
                value = truth + f["value"] * (self.t - f["start_sim"])
                if self.devices[sid]["type"] == "traffic_camera":
                    value = max(0, int(round(value)))
                return round(value, 3), True, label
            if k == "flood_network":
                return truth, True, label
            return f["value"], True, label     # spoof, spike

    # ---------- emergency calls ----------
    def nearest_zone(self, sensor, lonlat):
        others = [d for d in self.devices.values() if d["zone"] != sensor["zone"]]
        if not others:
            return sensor["zone"]
        return min(others, key=lambda d: (d["lonlat"][0] - lonlat[0]) ** 2 + (d["lonlat"][1] - lonlat[1]) ** 2)["zone"]

    def nearest_road(self, lonlat):
        if not self.net:
            return ""
        near = self.net.getNeighboringEdges(*self.net.convertLonLat2XY(*lonlat), 200)
        return min(near, key=lambda e: e[1])[0].getName() if near else ""

    def record_call(self, sensor, call_type, lonlat, severity, description="", road=None, genuine=True, zone_id=None):
        """Publish and store one call. The caller's identity is never recorded."""
        number = next(self.call_numbers)
        call_id = "CALL-%06d" % number
        self.truth[call_id] = "genuine" if genuine else "fake"   # never published
        dialled, service, _ = CALL_TYPES[call_type]
        msg = {"contract_version": CONTRACT, "type": "reading", "sensor_id": sensor["id"],
               "zone_id": zone_id or self.nearest_zone(sensor, lonlat), "kind": "emergency_call", "value": 1, "unit": "call",
               "ts": now_iso(), "seq": number, "call_id": call_id, "sim_time": self.t, "number_dialled": dialled,
               "service": service, "call_type": call_type, "severity": severity, "description": description,
               "lonlat": [round(lonlat[0], 5), round(lonlat[1], 5)],
               "road": self.nearest_road(lonlat) if road is None else road}
        msg["sig"] = self.sign(msg)
        _, delivered, _ = self.apply_fault(sensor["id"], 1)    # only a dropout fault is allowed on this sensor
        if delivered:
            self.last[sensor["id"]] = msg
            self.calls.append(msg)
            self.publish("city/%s/emergency_call/%s" % (sensor["zone"], sensor["id"]), msg)
        return msg

    def simulate_call(self, sensor):
        rng = self.call_rng
        call_type = rng.choices(list(CALL_TYPES), [v[2] for v in CALL_TYPES.values()])[0]
        severity = rng.choices(["low", "medium", "high"], [40, 40, 20])[0]
        west, south, east, north = sensor["bbox"]
        road = ""
        if self.net:
            roads = self.call_edges.get(sensor["id"])
            if roads is None:
                roads = []
                for e in self.net.getEdges():
                    shape = e.getShape()
                    spot = self.net.convertXY2LonLat(*shape[len(shape) // 2])
                    if e.allows("passenger") and in_bbox(spot, sensor["bbox"]):
                        roads.append((spot, e.getName()))
                self.call_edges[sensor["id"]] = roads
            lonlat, road = rng.choice(roads)
        else:
            lonlat = (rng.uniform(west, east), rng.uniform(south, north))
        self.record_call(sensor, call_type, lonlat, severity, "Simulated %s call" % call_type, road=road)

    # ---------- readings ----------
    def period(self, d):
        return float(self.cfg.get("publish_every_s_by_kind", {}).get(d["type"], self.cfg["publish_every_s"]))

    def make_reading(self, d, value, fault_label, fields=None):
        sid = d["id"]
        self.seq[sid] = self.seq.get(sid, 0) + 1
        msg = {"contract_version": CONTRACT, "type": "reading", "sensor_id": sid, "zone_id": d["zone"],
               "kind": d["type"], "value": value, "unit": UNITS[d["type"]], "ts": now_iso(), "seq": self.seq[sid]}
        if fields:
            msg.update(fields)
        if d["type"] == "air_quality":
            msg["aqi"] = pm25_to_aqi(value)
        if sid in self.quarantined:
            msg["quarantined"] = True
        if fault_label:
            msg["fault"] = fault_label
        msg["sig"] = self.sign(msg)
        return msg

    def camera_fields(self, sid, value, fault_label):
        """Camera details that match the published count, faked or not."""
        if fault_label and fault_label["kind"] == "stuck":
            f = self.faults.get(sid, {})
            f.setdefault("held_fields", {k: self.last.get(sid, {}).get(k) for k in CAMERA_FIELDS})
            return dict(f["held_fields"])
        if fault_label and fault_label["kind"] in ("spoof", "spike", "drift") and isinstance(value, (int, float)):
            return camera_from_count(value, self.camera_capacity(sid), self.camera_limit(sid))
        return dict(self.truth_fields.get(sid, {k: None for k in CAMERA_FIELDS}))

    def publish_sensors(self):
        for d in self.devices.values():
            if d["class"] != "sensor" or d["source"] == "esp32":
                continue
            if d["type"] == "emergency_call":
                chance = d.get("calls_per_hour", 0) * self.cfg["publish_every_s"] / 3600.0
                if d["source"] == "sim" and self.call_rng.random() < chance:
                    self.simulate_call(d)
                continue
            sid = d["id"]
            if self.t < self.next_due.get(sid, 0.0):
                continue
            self.next_due[sid] = max(self.next_due.get(sid, 0.0) + self.period(d), self.t)
            truth = self.read_truth(d)
            if truth is None:   # not attached to a road; pick_edge already warned at startup
                continue
            self.truth[sid] = truth
            topic = "city/%s/%s/%s" % (d["zone"], d["type"], sid)
            with self.fault_lock:
                f = self.faults.get(sid)
                if f and f["kind"] == "replay":
                    self.replay_one(sid, f, topic)
                    continue
            value, ok, fault_label = self.apply_fault(sid, truth)
            if not ok:
                continue
            fields = self.camera_fields(sid, value, fault_label) if d["type"] == "traffic_camera" else None
            msg = self.make_reading(d, value, fault_label, fields)
            self.last[sid] = msg
            if not fault_label:
                self.history.setdefault(sid, deque(maxlen=REPLAY_BUFFER)).append(msg)
            self.publish(topic, msg)
            if fault_label and fault_label["kind"] == "flood_network":
                for _ in range(min(MAX_FLOOD_RATE, max(1, int(f["value"] or 20)))):
                    self.publish(topic, self.make_reading(d, value, fault_label, fields))

    def replay_one(self, sid, f, topic):
        """Replay attack: the genuine reading is held back and an old, correctly signed message is sent instead,
        unchanged (old seq, old ts). The device itself keeps counting, so its seq is higher when the attack ends."""
        self.seq[sid] = self.seq.get(sid, 0) + 1
        if not f["replay"]:
            return                      # nothing recorded yet: the sensor simply goes quiet
        msg = f["replay"][f["replay_i"] % len(f["replay"])]
        f["replay_i"] += 1
        self.last[sid] = msg
        self.publish(topic, msg)

    # ---------- dispatch ----------
    def zone_center(self, zone):
        pts = [d["lonlat"] for d in self.devices.values() if d["zone"] == zone and d.get("lonlat")]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def route(self, a, b):
        """(distance in metres, how it was found). Uses the SUMO road graph when there is one."""
        disp = self.cfg.get("dispatch", {})
        if self.net:
            try:
                ea = min(self.net.getNeighboringEdges(*self.net.convertLonLat2XY(*a), 300), key=lambda e: e[1])[0]
                eb = min(self.net.getNeighboringEdges(*self.net.convertLonLat2XY(*b), 300), key=lambda e: e[1])[0]
                path, _ = self.net.getShortestPath(ea, eb, vClass="passenger")
                if path:
                    return sum(e.getLength() for e in path), "sumo_route"
            except (ValueError, TypeError):
                pass
        return haversine_m(a, b) * float(disp.get("road_factor", 1.3)), "estimate"

    def dispatch(self, aid, cmd_id, args):
        """Returns (ok, state, detail, extra feedback fields)."""
        disp = self.cfg.get("dispatch", {})
        free = [u for u in self.units.values() if u["type"] == args["type"]
                and (args.get("unit_id") in (None, u["id"]))
                and (u["busy_until"] is None or u["busy_until"] <= self.t)]
        if not free:
            return False, "no_unit", "no %s unit available%s" % (args["type"], " (%s is busy or unknown)" % args["unit_id"] if args.get("unit_id") else ""), {"unit_type": args["type"], "target_zone": args["zone_id"]}
        target = self.zone_center(args["zone_id"])
        unit = min(free, key=lambda u: haversine_m(u["lonlat"], target))
        dist, how = self.route(unit["lonlat"], target)
        eta = round(dist / (float(disp.get("speed_kmh", 40)) / 3.6), 1)
        unit["busy_until"] = self.t + eta + float(disp.get("on_scene_s", 120))
        extra = {"unit_id": unit["id"], "unit_type": unit["type"], "target_zone": args["zone_id"], "eta_s": eta,
                 "distance_m": round(dist), "route": how}
        self.arrivals.append({"t": self.t + eta, "aid": aid, "cmd_id": cmd_id, "extra": extra})
        return True, "dispatched", "%s on its way to %s, about %.0f s" % (unit["id"], args["zone_id"], eta), extra

    def recall_unit(self, cmd_id, uid):
        """Free a unit now (the Brain gave its mission to a more urgent incident). Main loop only."""
        unit = self.units[uid]
        was_busy = unit["busy_until"] is not None and unit["busy_until"] > self.t
        self.arrivals = [a for a in self.arrivals if a["extra"]["unit_id"] != uid]
        unit["busy_until"] = None
        aid = next(i for i, d in self.devices.items() if d["type"] == "emergency_dispatch")
        self.send_feedback(aid, cmd_id, "recalled", True, "%s recalled%s" % (uid, "" if was_busy else " (it was free)"),
                           {"unit_id": uid, "was_busy": was_busy})

    def process_arrivals(self):
        for a in [a for a in self.arrivals if a["t"] <= self.t]:
            self.arrivals.remove(a)
            self.send_feedback(a["aid"], a["cmd_id"], "arrived", True, "%s arrived in %s" % (a["extra"]["unit_id"], a["extra"]["target_zone"]),
                               a["extra"])

    # ---------- actuators ----------
    def send_feedback(self, aid, cmd_id, state, ok, detail, extra=None, fault=None, verdict_ref=None):
        d = self.devices[aid]
        fb = {"contract_version": CONTRACT, "type": "feedback", "cmd_id": cmd_id, "actuator_id": aid,
              "zone_id": d["zone"], "state": state, "ok": ok, "detail": detail, "ts": now_iso()}
        if extra:
            fb.update(extra)
        if verdict_ref:
            fb["verdict_ref"] = verdict_ref
        if fault:
            fb["fault"] = {"kind": fault["kind"], "id": fault["id"]}
        fb["sig"] = self.sign(fb)
        self.publish("city/%s/actuator/%s/state" % (d["zone"], aid), fb)
        return fb

    def apply_command(self, aid, cmd_id, args, verdict_ref=None):
        d = self.devices[aid]
        typ, ok, detail, extra = d["type"], True, "", {}
        with self.fault_lock:
            f = self.faults.get(aid)
        stuck = f is not None and f["kind"] == "actuator_stuck"
        silent = stuck and bool(f.get("value"))     # value 1: reports success but does nothing
        state = self.actuator_state[aid]
        if stuck and not silent:
            ok, detail = False, "actuator did not respond (injected fault)"
        else:
            try:
                if typ == "traffic_signal":
                    if self.traci and aid in self.tls and not silent:
                        self.traci.trafficlight.setPhase(self.tls[aid], int(args["phase"]))
                    state = "phase_%s" % args["phase"]
                elif typ == "road_closure":
                    closed = args["state"] == "closed"
                    if self.traci and aid in self.edges and not silent:
                        self.traci.edge.setDisallowed(self.edges[aid], ["passenger"] if closed else [])
                    state = args["state"]
                elif typ == "water_valve":
                    if not silent:
                        self.valve_open[aid] = args["state"] == "open"
                    state = args["state"]
                elif typ == "public_alert":
                    state = "alert_%s" % args.get("level", "info")
                    print("[ALERT] %s (%s): %s" % (d["zone"], args.get("level"), args.get("msg", args.get("text", ""))))
                    extra = {"level": args.get("level"), "msg": args.get("msg", args.get("text", ""))}
                elif typ == "power_grid":
                    if not silent:
                        self.grid_action[d["zone"]] = args["action"]
                    state = args["action"]
                elif typ == "emergency_dispatch":
                    ok, state, detail, extra = self.dispatch(aid, cmd_id, args)
                elif typ == "device_quarantine":
                    sid = args["sensor_id"]
                    want = args.get("state", "quarantined")
                    (self.quarantined.add if want == "quarantined" else self.quarantined.discard)(sid)
                    state, extra = want, {"sensor_id": sid}
                    detail = "%s %s by the Guardian" % (sid, want)
                else:
                    raise ValueError("unknown actuator type")
            except (KeyError, ValueError) as e:
                ok, state, detail = False, self.actuator_state[aid], "invalid args: %s" % e
        if ok and typ != "emergency_dispatch":
            self.actuator_state[aid] = state
        elif ok:
            self.actuator_state[aid] = "%d units busy" % sum(1 for u in self.units.values()
                                                             if u["busy_until"] and u["busy_until"] > self.t)
        self.send_feedback(aid, cmd_id, state if ok else self.actuator_state[aid], ok, detail, extra,
                           f if stuck else None, verdict_ref)

    def apply_reset(self):
        """Test baseline (main loop only, because it touches SUMO)."""
        self.grid_action = {z: "normal" for z in self.zones}
        for i in self.water:
            self.water[i] = 0.4
        self.arrivals.clear()
        for u in self.units.values():
            u["busy_until"] = None
        for aid, d in self.devices.items():
            want = {"water_valve": "closed", "road_closure": "open", "power_grid": "normal"}.get(d["type"])
            if want is None:
                continue
            if d["type"] == "water_valve":
                self.valve_open[aid] = False
            if d["type"] == "road_closure" and self.traci and aid in self.edges:
                self.traci.edge.setDisallowed(self.edges[aid], [])
            if self.actuator_state[aid] != want:
                self.actuator_state[aid] = want
                self.send_feedback(aid, "scenario-reset", want, True, "baseline restored by POST /scenario/reset")
        if self.traci:
            self.apply_jam_sumo()

    def validation_ctx(self):
        return {"zones": self.zones, "unit_types": sorted({u["type"] for u in self.units.values()}) or None,
                "sensors": [i for i, d in self.devices.items() if d["class"] == "sensor"]}

    def resolve_tool(self, tool, args):
        """Tool call (Phase 0 contract names) -> (actuator id, actuator args), or raises ValueError with the reason."""
        spec = TOOLS.get(tool)
        if spec is None:
            raise ValueError("unknown tool %s; known tools: %s" % (tool, ", ".join(sorted(TOOLS))))
        typ = spec["type"]
        if typ in ("emergency_dispatch", "device_quarantine"):
            ids = [i for i, d in self.devices.items() if d["type"] == typ]
            if not ids:
                raise ValueError("no %s device in the configuration" % typ)
            if typ == "device_quarantine":
                return ids[0], {"sensor_id": args.get("sensor_id"),
                                "state": "released" if tool == "release_device" else "quarantined"}
            return ids[0], {"type": args.get("type"), "zone_id": args.get("zone_id")}
        aid = args.get("actuator_id")
        if aid is None:
            zone = args.get("zone_id")
            ids = sorted(i for i, d in self.devices.items() if d["type"] == typ and d["zone"] == zone)
            if not ids:
                raise ValueError("no %s device in zone %s" % (typ, json.dumps(zone)))
            aid = ids[0]
        elif self.devices.get(aid, {}).get("type") != typ:
            raise ValueError("%s is not a %s" % (aid, typ))
        out = {k: v for k, v in args.items() if k not in ("zone_id", "actuator_id")}
        if typ == "public_alert" and "text" in out:
            out["msg"] = out.pop("text")
        return aid, out

    # ---------- main loop ----------
    def step_once(self):
        while True:
            try:
                item = self.cmds.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__reset__":
                self.apply_reset()
            elif item[0] == "__recall__":
                self.recall_unit(item[1], item[2])
            else:
                self.apply_command(*item)
        if self.traci:
            if self.conditions_dirty:
                self.conditions_dirty = False
                self.apply_jam_sumo()
            self.traci.simulationStep()
        self.t += self.cfg["sumo"]["step_length"]
        self.expire_faults()
        self.process_arrivals()
        self.publish_sensors()
        if self.bridge:
            self.bridge.tick()

    def run(self):
        step = self.cfg["sumo"]["step_length"]
        while self.running:
            self.step_once()
            time.sleep(step / self.speed)


# ---------- REST API ----------
class Fault(BaseModel):
    sensor_id: str
    kind: Literal["stuck", "dropout", "spoof", "spike", "drift", "replay", "flood_network", "actuator_stuck"]
    value: float | None = None
    duration_s: float | None = Field(default=None, gt=0)


class EmergencyCall(BaseModel):
    call_type: Literal["medical", "accident", "crime", "fire", "flood"]
    lonlat: tuple[float, float]       # [longitude, latitude]
    severity: Literal["low", "medium", "high"] = "medium"
    description: str = Field(default="", max_length=500)
    genuine: bool = True              # false = a fake call; only visible through /truth, for scoring


class Conditions(BaseModel):
    zone_id: str | None = None        # None = every zone
    rain_mm_h: float | None = Field(default=None, ge=0, le=300)
    jam: float | None = Field(default=None, ge=0, le=1)
    pollution: float | None = Field(default=None, ge=0.1, le=20)
    power_demand: float | None = Field(default=None, ge=0.1, le=3)


class SchemaInvalid(Exception):
    pass


ERROR_CODES = {400: "BAD_ARGS", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 422: "BAD_ARGS", 503: "UNAVAILABLE"}


def build_app(twin):
    app = FastAPI(title="TSYP Twin", version=CONTRACT)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        return JSONResponse(error_body(ERROR_CODES.get(exc.status_code, "INTERNAL"), detail), status_code=exc.status_code)

    @app.exception_handler(SchemaInvalid)
    async def schema_error(request: Request, exc: SchemaInvalid):
        return JSONResponse(error_body("SCHEMA_INVALID", str(exc)), status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = ["%s: %s" % (".".join(str(p) for p in e.get("loc", []) if p != "body") or "(body)", e.get("msg"))
                  for e in exc.errors()]
        return JSONResponse(error_body("SCHEMA_INVALID", "; ".join(errors), errors), status_code=422)

    def key(x_api_key: str = Header(default="")):
        return x_api_key

    def role_for(k):
        """Per-layer keys: api_key = Brain, guardian_key = Guardian, test_key = test scripts and scoring."""
        a = twin.cfg["api"]
        for role, name in (("brain", "api_key"), ("guardian", "guardian_key"), ("test", "test_key")):
            if a.get(name) and k == a[name]:
                return role
        raise HTTPException(401, "invalid or missing X-API-Key")

    def require(*roles, what):
        def dep(x_api_key: str = Header(default="")):
            role = role_for(x_api_key)
            if role not in roles:
                raise HTTPException(403, "the %s key may not %s; allowed: %s key" % (role, what, " or ".join(roles)))
            return role
        return dep

    test_auth = require("test", what="inject faults, change conditions or read ground truth")
    read_auth = require("brain", "test", what="read this")
    guardian_auth = require("guardian", what="quarantine or release a device")
    calls_auth = require("test", what="create emergency calls")

    def accept(aid, cmd_id, args, verdict_ref, role):
        """Validate, queue, and acknowledge one command."""
        d = twin.devices[aid]
        if d["type"] == "device_quarantine" and role != "guardian":
            raise HTTPException(403, "only the Guardian may quarantine or release a device")
        if d["type"] != "device_quarantine" and role != "brain":
            raise HTTPException(403, "only the Brain key may command actuators (this is the %s key)" % role)
        error = command_error(aid, d["type"], args, twin.signal_phases.get(aid, DEFAULT_SIGNAL_PHASES), twin.validation_ctx())
        if error:
            raise HTTPException(422, error)
        twin.cmds.put((aid, cmd_id, args, verdict_ref))
        ack = {"contract_version": CONTRACT, "type": "ack", "cmd_id": cmd_id, "actuator_id": aid, "zone_id": d["zone"],
               "accepted": True, "verdict_ref": verdict_ref, "ts": now_iso(),
               "note": "result is published as feedback on MQTT city/%s/actuator/%s/state" % (d["zone"], aid)}
        ack["sig"] = twin.sign(ack)
        twin.publish("city/%s/actuator/%s/ack" % (d["zone"], aid), ack)
        return ack

    def check_command_body(body):
        problems = twin.schemas.errors("command", body)
        if problems:
            raise SchemaInvalid("command does not match docs/schemas/command.schema.json: " + "; ".join(problems))

    @app.get("/health")
    def health():
        return {"ok": True, "sim_time": twin.t, "fake": twin.fake, "mqtt": twin.mqtt is not None,
                "contract_version": CONTRACT}

    @app.get("/devices")
    def devices():
        return [{k: v for k, v in d.items() if k != "xy"} for d in twin.devices.values()]

    @app.get("/sensors/{sid}")
    def sensor(sid: str):
        if sid not in twin.last:
            raise HTTPException(404, "no reading yet for %s" % sid)
        return twin.last[sid]

    @app.get("/actuators/{aid}", dependencies=[Depends(read_auth)])
    def actuator(aid: str):
        if aid not in twin.actuator_state:
            raise HTTPException(404, "unknown actuator")
        return {"actuator_id": aid, "state": twin.actuator_state[aid]}

    @app.post("/actuators/{aid}")
    def command(aid: str, body: dict = Body(...), k: str = Depends(key)):
        role = role_for(k)
        if aid not in twin.actuator_state:
            raise HTTPException(404, "unknown actuator %s" % aid)
        check_command_body(body)
        return accept(aid, body["cmd_id"], body["args"], body.get("verdict_ref"), role)

    @app.get("/tools")
    def tools():
        return [{"tool": name, "actuator_type": s["type"], "args": s["args"]} for name, s in TOOLS.items()]

    @app.post("/tools/{tool}")
    def tool_call(tool: str, body: dict = Body(...), k: str = Depends(key)):
        role = role_for(k)
        check_command_body(body)
        try:
            aid, args = twin.resolve_tool(tool, body["args"])
        except ValueError as e:
            raise HTTPException(404 if "unknown tool" in str(e) else 422, str(e))
        return accept(aid, body["cmd_id"], args, body.get("verdict_ref"), role)

    def quarantine_device_id():
        ids = [i for i, d in twin.devices.items() if d["type"] == "device_quarantine"]
        if not ids:
            raise HTTPException(404, "no device_quarantine actuator in the configuration")
        return ids[0]

    @app.post("/quarantine/{sid}", dependencies=[Depends(guardian_auth)])
    def quarantine(sid: str, verdict_ref: str | None = None):
        return accept(quarantine_device_id(), "q-" + uuid.uuid4().hex[:8], {"sensor_id": sid, "state": "quarantined"},
                      verdict_ref, "guardian")

    @app.delete("/quarantine/{sid}", dependencies=[Depends(guardian_auth)])
    def release(sid: str):
        return accept(quarantine_device_id(), "r-" + uuid.uuid4().hex[:8], {"sensor_id": sid, "state": "released"},
                      None, "guardian")

    @app.get("/quarantine")
    def quarantined():
        return sorted(twin.quarantined)

    @app.post("/faults", dependencies=[Depends(test_auth)])
    def fault(f: Fault):
        d = twin.devices.get(f.sensor_id)
        if d is None:
            raise HTTPException(404, "unknown device")
        if d["class"] == "actuator":
            allowed = {"actuator_stuck"}
        elif d["source"] == "esp32":
            allowed = set()    # a real device publishes for itself, so the twin cannot change its readings
        elif d["type"] == "emergency_call":
            allowed = {"dropout"}
        else:
            allowed = set(SENSOR_FAULTS)
        if f.kind not in allowed:
            raise HTTPException(422, "fault %s cannot be applied to %s; allowed: %s" % (f.kind, f.sensor_id, sorted(allowed)))
        if f.kind in ("spoof", "spike", "drift") and f.value is None:
            raise HTTPException(422, "fault %s needs a value%s" % (f.kind, " (drift rate per simulated second)" if f.kind == "drift" else ""))
        if f.kind == "flood_network" and f.value is not None and not 1 <= f.value <= MAX_FLOOD_RATE:
            raise HTTPException(422, "flood_network value is extra messages per reading, from 1 to %d" % MAX_FLOOD_RATE)
        started = twin.start_fault(f.sensor_id, f.kind, f.value, f.duration_s)
        return {"injected": f.kind, "sensor_id": f.sensor_id, "until_sim_time": started["until"],
                "fault_id": started["id"]}

    @app.delete("/faults/{sid}", dependencies=[Depends(test_auth)])
    def clear_fault(sid: str):
        ended = twin.end_fault(sid, "cleared")
        return {"cleared": sid, "fault_id": ended["id"] if ended else None}

    @app.get("/faults", dependencies=[Depends(test_auth)])
    def list_faults():
        with twin.fault_lock:
            return [{k: v for k, v in f.items() if k not in ("replay", "held_fields")} for f in twin.faults.values()]

    @app.post("/scenario/rain/{mm_h}", dependencies=[Depends(test_auth)])
    def rain(mm_h: float):
        twin.rain_mm_h = max(0.0, mm_h)
        return {"rain_mm_h": twin.rain_mm_h}

    @app.get("/scenario/conditions", dependencies=[Depends(test_auth)])
    def get_conditions():
        return {"rain_mm_h": twin.rain_mm_h, "zones": twin.conditions, "grid": twin.grid_action}

    @app.post("/scenario/conditions", dependencies=[Depends(test_auth)])
    def set_conditions(c: Conditions):
        if c.zone_id is not None and c.zone_id not in twin.zones:
            raise HTTPException(422, "zone_id must be one of %s" % ", ".join(twin.zones))
        for z in ([c.zone_id] if c.zone_id else twin.zones):
            for k in ("rain_mm_h", "jam", "pollution", "power_demand"):
                v = getattr(c, k)
                if v is not None:
                    twin.conditions[z][k] = v
        if c.zone_id is None and c.rain_mm_h is not None:
            twin.rain_mm_h = c.rain_mm_h
        twin.conditions_dirty = True
        return get_conditions()

    @app.post("/scenario/reset", dependencies=[Depends(test_auth)])
    def reset():
        """Back to a calm baseline: no rain, jam, smog or demand peak; faults cleared; water at its normal level;
        valves closed, roads open, grids normal, emergency units back at base. Quarantine is left to the Guardian.
        Applied by the main loop at the next step (it touches SUMO), with feedback for every actuator it changes."""
        twin.rain_mm_h = 0.0
        twin.conditions = {z: twin.default_conditions() for z in twin.zones}
        twin.conditions_dirty = True
        for sid in list(twin.faults):
            twin.end_fault(sid, "cleared")
        twin.cmds.put(("__reset__", None, None, None))
        return get_conditions()

    @app.get("/contract")
    def contract():
        """What a partner needs to connect: contract version, topics, endpoints, key roles and schemas."""
        endpoints = sorted({"%s %s" % (m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or [])
                            if m not in ("HEAD", "OPTIONS") and not r.path.startswith(("/docs", "/redoc", "/openapi"))})
        return {"contract_version": CONTRACT, "layer": "twin",
                "mqtt": {"host": twin.cfg["mqtt"]["host"], "port": twin.cfg["mqtt"]["port"],
                         "publishes": {"city/<zone>/<kind>/<sensor_id>": "reading",
                                       "city/<zone>/actuator/<actuator_id>/ack": "ack",
                                       "city/<zone>/actuator/<actuator_id>/state": "feedback",
                                       "city/faults": "fault_event (scoring only; a Guardian under test must ignore it)"},
                         "expects_from_partners": {"guardian/verdicts/<sensor_id>": "verdict", "brain/decisions": "decision"}},
                "zones": twin.zones, "sensor_kinds": sorted(UNITS), "tools": sorted(TOOLS),
                "endpoints": endpoints,
                "keys": {"api_key (Brain)": "command actuators, read actuator state and emergency calls",
                         "guardian_key (Guardian)": "quarantine and release devices only",
                         "test_key (test scripts)": "faults, conditions, reset, emergency calls, /truth, /events"},
                "signature": "sig = HMAC-SHA256(hmac_key, json.dumps(msg without sig, sort_keys=True, separators=(',', ':'))) as hex",
                "schemas": sorted(twin.schemas.validators), "schemas_path": "docs/schemas/"}

    @app.get("/truth/{sid}", dependencies=[Depends(test_auth)])
    def truth(sid: str):
        """Ground truth, for scoring tests only. The Brain and Guardian must never call this."""
        out = {"sensor_id": sid, "true_value": twin.truth.get(sid)}
        if sid in twin.truth_fields:
            out["true_fields"] = twin.truth_fields[sid]
        with twin.fault_lock:
            f = twin.faults.get(sid)
        out["active_fault"] = {"kind": f["kind"], "id": f["id"]} if f else None
        out["quarantined"] = sid in twin.quarantined
        return out

    @app.get("/events", dependencies=[Depends(test_auth)])
    def events(limit: int = 100):
        return twin.log.read_recent(limit)

    @app.post("/emergency_calls", dependencies=[Depends(calls_auth)])
    def new_call(c: EmergencyCall):
        sensor = next((d for d in twin.devices.values() if d["type"] == "emergency_call"), None)
        if sensor is None:
            raise HTTPException(404, "no emergency_call sensor in the configuration")
        if not in_bbox(c.lonlat, sensor["bbox"]):
            raise HTTPException(422, "lonlat %s is outside the city area %s" % (list(c.lonlat), sensor["bbox"]))
        msg = twin.record_call(sensor, c.call_type, c.lonlat, c.severity, c.description, genuine=c.genuine)
        return {"accepted": True, "call_id": msg["call_id"], "zone_id": msg["zone_id"], "road": msg["road"]}

    @app.get("/emergency_calls", dependencies=[Depends(read_auth)])
    def list_calls(limit: int = 100, zone: str | None = None, call_type: str | None = None):
        rows = [c for c in twin.calls.read()[::-1]
                if (zone is None or c["zone_id"] == zone) and (call_type is None or c["call_type"] == call_type)]
        return rows[:max(limit, 0)]

    return app


def load_config(path):
    with open(path, encoding="utf-8-sig") as f:      # utf-8-sig: a byte-order mark no longer breaks the first setting
        return apply_integration_env(apply_env(yaml.safe_load(f)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "city.yaml"))
    ap.add_argument("--fake", action="store_true", help="run without SUMO")
    ap.add_argument("--speed", type=float, default=1.0, help="simulation speed vs real time")
    a = ap.parse_args()
    cfg = load_config(a.config)
    twin = Twin(cfg, os.path.dirname(os.path.abspath(a.config)), a.fake, a.speed)
    if not a.fake:
        twin.start_sumo()
    if cfg.get("integration", {}).get("enabled"):
        from bridge import Bridge
        twin.bridge = Bridge(twin, cfg["integration"], os.path.dirname(os.path.abspath(a.config)))
        twin.bridge.start()
    server = uvicorn.Server(uvicorn.Config(build_app(twin), host=cfg["api"]["host"], port=int(cfg["api"]["port"]),
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    print("Twin running. API: http://127.0.0.1:%s/docs   (fake=%s)" % (cfg["api"]["port"], a.fake))
    try:
        twin.run()
    except KeyboardInterrupt:
        pass
    finally:
        twin.running = False
        if twin.bridge:
            twin.bridge.stop()
        for sid in list(twin.faults):
            twin.end_fault(sid, "twin_stopped")
        if twin.traci:
            twin.traci.close()


if __name__ == "__main__":
    main()
