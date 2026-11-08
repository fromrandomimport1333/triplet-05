"""City Brain protocol profile, part 2: translation between the Twin's native messages and the partners' bodies.

Pure functions over data (no MQTT, no threads), driven entirely by the `integration:` section of city.yaml.
Whitelisting is deliberate: only the fields of the partners' contract leave the Twin, so test labels (`fault`,
`/truth`, `city/faults`) can never reach the Brain or the Guardian.
"""
import math

from protocol import ProtocolError, iso_ms, normalize_id


class Reject(Exception):
    """A Brain command the Twin cannot execute; `reason` goes into the `rejected` acknowledgement."""


def _zone_of(lonlat, devices):
    near = min((d for d in devices.values() if d.get("lonlat") and d["zone"] != "city"),
               key=lambda d: (d["lonlat"][0] - lonlat[0]) ** 2 + (d["lonlat"][1] - lonlat[1]) ** 2)
    return near["zone"]


class CityMap:
    """The city as the partners see it: ids, zones, sensors, actuators, units, and the way back to native ids."""

    def __init__(self, icfg, devices, dispatch_cfg, run_id):
        self.icfg, self.devices, self.run_id = icfg, devices, run_id
        self.n = 0
        id_map = icfg.get("id_map") or {}
        self.ex = lambda native: id_map.get(native) or normalize_id(native)
        self.zones = sorted({d["zone"] for d in devices.values() if d["zone"] != "city"})
        self.zone_native = {self.ex(z): z for z in self.zones}
        self.sensors = {}        # exposed sensor id -> spec dict (native id, type, unit, field, scale, ...)
        self.by_native = {}      # native sensor id -> [exposed ids]
        for nid, d in devices.items():
            if d["class"] != "sensor":
                continue
            for spec in (icfg.get("sensors", {}).get(d["type"]) or []):
                if spec.get("expose", True) is False:
                    continue
                eid = self.ex(nid) + (("-" + spec["id_suffix"]) if spec.get("id_suffix") else "")
                self.sensors[eid] = dict(spec, native=nid, zone=d["zone"])
                self.by_native.setdefault(nid, []).append(eid)
        self.actuators, self.actuator_native = {}, {}
        amap = icfg.get("actuators") or {}
        for nid, d in devices.items():
            if d["class"] == "actuator" and d["type"] in amap:
                eid = self.ex(nid)
                self.actuators[eid] = dict(amap[d["type"]], native=nid, zone=d["zone"], twin_type=d["type"])
                self.actuator_native[nid] = eid
        tmap = icfg.get("unit_types") or {}
        self.units, self.unit_native = {}, {}
        for u in dispatch_cfg.get("units", []):
            eid = self.ex(u["id"])
            self.units[eid] = {"native": u["id"], "twin_type": u["type"], "type": tmap.get(u["type"], u["type"]),
                               "home_zone": _zone_of(u["lonlat"], devices)}
            self.unit_native[u["id"]] = eid
        self.dispatcher = next((i for i, d in devices.items() if d["type"] == "emergency_dispatch"), None)

    # ---- ids ----
    def next_id(self, prefix):
        self.n += 1
        return "%s-%s-%06d" % (self.run_id, prefix, self.n)

    def common(self, prefix, correlation_id=None, **fields):
        body = {"id": self.next_id(prefix), "timestamp": iso_ms(), "source": "twin"}
        if correlation_id:
            body["correlation_id"] = correlation_id
        body.update(fields)
        return body

    def device_native(self, exposed):
        """Exposed device id (sensor, actuator or unit) -> ("sensor"|"actuator"|"unit", native id), or None."""
        if exposed in self.sensors:
            return "sensor", self.sensors[exposed]["native"]
        if exposed in self.actuators:
            return "actuator", self.actuators[exposed]["native"]
        if exposed in self.units:
            return "unit", self.units[exposed]["native"]
        return None

    # ---- twin/meta ----
    def meta(self):
        zcfg = self.icfg.get("zones") or {}
        zones = []
        for z in self.zones:
            c = zcfg.get(z) or {}
            adj = c.get("adjacent") if c.get("adjacent") is not None else [o for o in self.zones if o != z]
            zones.append({"zone_id": self.ex(z), "name": c.get("name", z), "type": c.get("type", "mixed"),
                          "exposure": c.get("exposure", "medium"), "adjacent": [self.ex(a) for a in adj]})
        sensors = []
        for eid, s in sorted(self.sensors.items()):
            decl = {"sensor_id": eid, "zone_id": self.ex(s["zone"]), "type": s["type"], "unit": s["unit"],
                    "min": s["min"], "max": s["max"]}
            if s.get("alert") is not None:
                decl["alert"] = s["alert"]
            sensors.append(decl)
        actuators = [{"actuator_id": eid, "type": a["type"], "zone_id": self.ex(a["zone"]),
                      "controls": [self.ex(a["zone"])], "actions": list(a["actions"])}
                     for eid, a in sorted(self.actuators.items())]
        units = [{"unit_id": eid, "type": u["type"], "home_zone": self.ex(u["home_zone"])}
                 for eid, u in sorted(self.units.items())]
        return self.common("meta", zones=zones, sensors=sensors, actuators=actuators, units=units)

    # ---- Twin -> partners ----
    def readings(self, msg):
        """One native reading -> [(topic, body)], one per exposed sensor (a camera gives its speed sensor, ...)."""
        out = []
        for eid in self.by_native.get(msg.get("sensor_id"), []):
            s = self.sensors[eid]
            raw = msg.get(s.get("field") or "value")
            if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw):
                continue
            seq = msg.get("seq")
            # The id derives from (sensor, seq): a replayed old message is a repeated id, as a real replay would be.
            body = {"id": "%s-%s-%s" % (self.run_id, eid, seq), "timestamp": msg.get("ts") or iso_ms(), "source": "twin",
                    "sensor_id": eid, "zone_id": self.ex(s["zone"]), "type": s["type"],
                    "value": round(raw * float(s.get("scale", 1.0)), 3), "unit": s["unit"], "origin": "simulated"}
            if isinstance(seq, int):
                body["seq"] = seq
            out.append(("city/%s/sensor/%s/%s" % (body["zone_id"], s["type"], eid), body))
        return out

    def call(self, msg):
        cats = self.icfg.get("call_categories") or {}
        zone = self.ex(msg["zone_id"])
        number = str(msg.get("call_id", "")).lower().replace("_", "-") or self.next_id("call")
        body = self.common("call", call_id=normalize_id(number), zone_id=zone,
                           category=cats.get(msg.get("call_type"), "other"),
                           text=(msg.get("description") or "")[:280] or None)
        if body["text"] is None:
            del body["text"]
        return "city/%s/call" % zone, body

    def actuator_state(self, native_id, state, correlation_id=None, result=None, reason=None):
        body = self.common("act", correlation_id, actuator_id=self.actuator_native[native_id], state=state)
        if result:
            body["result"] = result
        if reason:
            body["reason"] = reason
        return "twin/actuator/%s" % body["actuator_id"], body

    def unit_message(self, unit_eid, correlation_id=None, result=None, status=None, zone=None, state=None, reason=None):
        body = self.common("unit", correlation_id, unit_id=unit_eid)
        for k, v in (("result", result), ("status", status), ("zone_id", zone and self.ex(zone)), ("state", state),
                     ("reason", reason)):
            if v:
                body[k] = v
        return "twin/unit/%s" % unit_eid, body

    def status(self, state):
        return "twin/status", self.common("status", status=state)

    # ---- Brain command -> native command ----
    def native_command(self, body):
        """Brain command body -> (native actuator id, native args, ("actuator"|"unit", exposed id)). Raises Reject."""
        target, action, params = body["target_id"], body["action"], body.get("parameters") or {}
        where = self.device_native(target)
        if where is None or where[0] == "sensor":
            raise ProtocolError("E_UNKNOWN_ID", "target %s is not an actuator or unit of this city" % target)
        if where[0] == "unit":
            u = self.units[target]
            if action == "recall":
                return self.dispatcher, {"recall": True, "unit_id": u["native"]}, ("unit", target)
            if action != "dispatch":
                raise Reject("unsupported_action: units accept dispatch and recall (got %s)" % action)
            zone = self.zone_native.get(params.get("zone_id"))
            if zone is None:
                raise Reject("unknown_zone: parameters.zone_id %r is not a zone of this city" % params.get("zone_id"))
            if self.dispatcher is None:
                raise Reject("no_dispatcher: this Twin has no emergency_dispatch device")
            return self.dispatcher, {"type": u["twin_type"], "zone_id": zone, "unit_id": u["native"]}, ("unit", target)
        a = self.actuators[target]
        if action not in a["actions"]:
            raise Reject("unsupported_action: %s accepts %s, got %s" % (target, ", ".join(a["actions"]), action))
        args = dict((a.get("map") or {}).get(action) or {})
        if not args:
            raise Reject("unsupported_action: no translation configured for %s on %s" % (action, target))
        opening = params.get("opening")
        if a["twin_type"] == "water_valve" and action == "open" and isinstance(opening, (int, float)) and opening <= 0:
            args = {"state": "closed"}
        return a["native"], args, ("actuator", target)
