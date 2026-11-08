"""Graph service: the Brain's memory of the city and of every event (team doc 02-graph, 5).

Independent of MQTT and HTTP: `handle(topic, payload, retained)` takes the internal
messages; the query methods (`context`, `zone`, `actuators`, `chain`, `view`, `health`)
return (http_status, json_object) and are served by http.py. One lock guards the graph,
because MQTT callbacks and HTTP threads run side by side.

Nodes and links (02-graph 5.1):
    zone NEXT_TO zone, sensor IN zone, actuator IN zone, actuator CONTROLS zone,
    unit BASED_IN zone, reading FROM sensor, verdict ON reading (or ON sensor),
    call IN zone, incident IN zone, incident EVIDENCE reading / call,
    proposal FOR incident, decision FOR incident, decision CHOSE proposal,
    action EXECUTES decision, action TARGETS actuator / unit, ack CONFIRMS action,
    quarantine ON device.
Every event node is indexed by its correlation_id (the external message that started the
chain); an ack, whose correlation_id is the Brain command id, is listed in the chain of the
action that carries that command id.

Normal readings are not stored: the latest value per sensor is kept on the sensor node,
and the last `pending` readings and verdicts in a ring. A reading becomes a node when it is
at alert level, when Guardian rejects or suspects it, or when an incident uses it.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from ..common import config
from ..common.log import EventLogger, iso_utc
from .history import History
from .store import CITY_KINDS, Graph, Node

STAGES = {"reading": 0, "verdict": 1, "call": 2, "incident": 3, "proposal": 4, "decision": 5,
          "action": 6, "ack": 7}

Answer = tuple[int, dict[str, Any]]


class GraphService:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        graph_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
    ):
        self.cfg = graph_cfg or config.load("graph")
        self.topics = topics_cfg or config.load("topics")
        # bad direction per sensor type, shared with detection (speed and flow alert when LOW)
        sensor_cfg = config.load("detection").get("sensor", {})
        self.directions: dict[str, str] = dict(sensor_cfg.get("direction") or {})
        self.default_direction: str = sensor_cfg.get("default_direction", "up")
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-graph", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.g = Graph(self.cfg["caps"])
        self.lock = threading.RLock()
        self.pending_readings: OrderedDict[str, dict[str, Any]] = OrderedDict()   # corr -> reading
        self.pending_verdicts: OrderedDict[str, dict[str, Any]] = OrderedDict()   # corr -> verdict
        self.actions_by_command: dict[str, str] = {}      # command id -> action node id
        self.layers: dict[str, str] = {}
        self.started = clock()
        self.messages = 0
        self.history = History(self.cfg.get("history"), self.log)   # Ida 2021 reference (tools/build_history.py)

    # --- inputs -------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        return [
            (t["meta"]["topic"], 1),
            (t["reading"]["topic"], 0),
            (t["call"]["topic"], 1),
            (t["trust"]["topic"].replace("{sensor_id}", "+"), 1),
            (t["quarantine"]["topic"].replace("{device_id}", "+"), 1),
            (t["ack"]["topic"], 1),
            (t["layer_status"]["topic"].replace("{layer}", "+"), 1),
            (t["incident"]["topic"], 1),
            (t["proposal"]["topic"], 1),
            (t["decision"]["topic"], 1),
            (t["outcome"]["topic"], 1),
        ]

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            self.log.warning("internal_message_not_json", topic=topic)
            return
        if not isinstance(msg, dict):
            return
        t = self.topics["internal"]
        routes = {
            t["meta"]["topic"]: self.on_meta,
            t["reading"]["topic"]: self.on_reading,
            t["call"]["topic"]: self.on_call,
            t["ack"]["topic"]: self.on_ack,
            t["incident"]["topic"]: self.on_incident,
            t["proposal"]["topic"]: self.on_proposal,
            t["decision"]["topic"]: self.on_decision,
            t["outcome"]["topic"]: self.on_outcome,
        }
        fn = routes.get(topic)
        if fn is None:
            for name, f in (("trust", self.on_trust), ("quarantine", self.on_quarantine),
                            ("layer_status", self.on_layer_status)):
                if topic.startswith(t[name]["topic"].split("{")[0]):
                    fn = f
                    break
        if fn is None:
            return
        try:
            with self.lock:
                self.messages += 1
                fn(msg)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])

    # city ------------------------------------------------------------------------------

    def on_meta(self, m: dict[str, Any]) -> None:
        now = self.clock()
        g = self.g
        keep: set[str] = set()
        for z in m["zones"]:
            n = g.put("zone", z["zone_id"], now, name=z.get("name"), type=z.get("type"),
                      exposure=z.get("exposure"), adjacent=sorted(z.get("adjacent") or []))
            keep.add(n.key)
        for z in m["zones"]:
            for other in z.get("adjacent") or []:
                a, b = g.get("zone", z["zone_id"]), g.get("zone", other)
                g.link(a, "NEXT_TO", b)
                g.link(b, "NEXT_TO", a)
        for s in m["sensors"]:
            n = g.put("sensor", s["sensor_id"], now, zone_id=s["zone_id"], type=s["type"], unit=s["unit"],
                      alert=s.get("alert"))
            g.link(n, "IN", g.get("zone", s["zone_id"]))
            keep.add(n.key)
        for a in m.get("actuators") or []:
            n = g.put("actuator", a["actuator_id"], now, zone_id=a["zone_id"], type=a["type"],
                      controls=list(a.get("controls") or [a["zone_id"]]), actions=list(a.get("actions") or []))
            n.attrs.setdefault("quarantined", False)
            g.link(n, "IN", g.get("zone", a["zone_id"]))
            for z in n.attrs["controls"]:
                g.link(n, "CONTROLS", g.get("zone", z))
            keep.add(n.key)
        for u in m.get("units") or []:
            n = g.put("unit", u["unit_id"], now, type=u["type"], home_zone=u.get("home_zone"))
            n.attrs.setdefault("status", "free")
            n.attrs.setdefault("quarantined", False)
            g.link(n, "BASED_IN", g.get("zone", u.get("home_zone") or ""))
            keep.add(n.key)
        for kind in CITY_KINDS:                       # a device no longer declared is dropped
            for n in list(g.of_kind(kind)):
                if n.key not in keep:
                    g.remove(n.key)
        self.log.info("meta_loaded", zones=g.count("zone"), sensors=g.count("sensor"),
                      actuators=g.count("actuator"), units=g.count("unit"))

    # readings and verdicts ----------------------------------------------------------------

    def on_reading(self, m: dict[str, Any]) -> None:
        corr = m.get("correlation_id") or m["id"]
        sensor = self.g.get("sensor", m["sensor_id"])
        latest = {"value": m["value"], "unit": m.get("unit"), "timestamp": m.get("timestamp"), "reading_id": corr}
        if sensor is not None:
            sensor.attrs["latest"] = latest
        self._pend(self.pending_readings, corr, m)
        if self.cfg.get("store_alert_readings", True) and sensor is not None and self._at_alert(sensor, m["value"]):
            self._store_reading(corr, "alert level")

    def _at_alert(self, sensor: Any, value: float) -> bool:
        alert = sensor.attrs.get("alert")
        if alert is None:
            return False
        direction = self.directions.get(sensor.attrs.get("type"), self.default_direction)
        return value <= alert if direction == "down" else value >= alert

    def on_trust(self, m: dict[str, Any]) -> None:
        sensor = self.g.get("sensor", m["sensor_id"])
        if sensor is not None:
            sensor.attrs["trust"] = {"verdict": m["verdict"], "score": m.get("trust_score")}
        corr = m.get("correlation_id")
        if not corr:
            return
        if self.g.get("reading", corr) is not None or m["verdict"] != "trusted":
            if m["verdict"] != "trusted":
                self._store_reading(corr, m["verdict"])
            self._store_verdict(m)
        else:
            self._pend(self.pending_verdicts, corr, m)

    def _pend(self, ring: OrderedDict, corr: str, m: dict[str, Any]) -> None:
        ring[corr] = m
        ring.move_to_end(corr)
        while len(ring) > int(self.cfg["pending"]):
            ring.popitem(last=False)

    def _store_reading(self, corr: str, why: str) -> Node | None:
        node = self.g.get("reading", corr)
        if node is not None:
            return node
        m = self.pending_readings.get(corr)
        if m is None:
            return None
        node = self.g.put("reading", corr, self.clock(), corr=corr, sensor_id=m["sensor_id"],
                          zone_id=m["zone_id"], type=m["type"], value=m["value"], unit=m.get("unit"),
                          timestamp=m.get("timestamp"), stored_because=why)
        self.g.link(node, "FROM", self.g.get("sensor", m["sensor_id"]))
        v = self.pending_verdicts.pop(corr, None)
        if v is not None:
            self._store_verdict(v)
        return node

    def _store_verdict(self, m: dict[str, Any]) -> None:
        corr = m.get("correlation_id")
        node = self.g.put("verdict", m["id"], self.clock(), corr=corr, sensor_id=m["sensor_id"],
                          verdict=m["verdict"], score=m.get("trust_score"), reason=m.get("reason"),
                          timestamp=m.get("timestamp"))
        reading = self.g.get("reading", corr) if corr else None
        self.g.link(node, "ON", reading or self.g.get("sensor", m["sensor_id"]))

    # calls, quarantine, layers, acks ----------------------------------------------------

    def on_call(self, m: dict[str, Any]) -> None:
        node = self.g.put("call", m["call_id"], self.clock(), corr=m.get("correlation_id"), zone_id=m["zone_id"],
                          category=m.get("category"), text=(m.get("text") or "")[:200], timestamp=m.get("timestamp"))
        self.g.link(node, "IN", self.g.get("zone", m["zone_id"]))

    def on_quarantine(self, m: dict[str, Any]) -> None:
        device = self.g.get("actuator", m["device_id"]) or self.g.get("unit", m["device_id"]) \
            or self.g.get("sensor", m["device_id"])
        quarantined = m["state"] == "quarantined"
        if device is not None:
            device.attrs["quarantined"] = quarantined
        node = self.g.put("quarantine", m["id"], self.clock(), corr=m.get("correlation_id"),
                          device_id=m["device_id"], state=m["state"], reason=m.get("reason"),
                          timestamp=m.get("timestamp"))
        self.g.link(node, "ON", device)

    def on_layer_status(self, m: dict[str, Any]) -> None:
        self.layers[m["layer"]] = m["status"]

    def on_ack(self, m: dict[str, Any]) -> None:
        target = self.g.get("actuator", m["target_id"]) or self.g.get("unit", m["target_id"])
        if target is not None:
            for f in ("state", "status", "zone_id"):
                if m.get(f):
                    target.attrs["current_zone" if f == "zone_id" else f] = m[f]
        if m.get("result") is None:
            return                                    # a plain state update, not an acknowledgement
        node = self.g.put("ack", m["id"], self.clock(), target_id=m["target_id"], result=m["result"],
                          state=m.get("state"), reason=m.get("reason"), command_id=m["correlation_id"],
                          timestamp=m.get("timestamp"))
        action_id = self.actions_by_command.get(m["correlation_id"])
        action = self.g.get("action", action_id) if action_id else None
        if action is not None:
            self.g.link(node, "CONFIRMS", action)
            if action.corr:
                self.g.index_corr(node, action.corr)

    # brain decisions -----------------------------------------------------------------------

    def on_incident(self, m: dict[str, Any]) -> None:
        g = self.g
        now = self.clock()
        node = g.get("incident", m["incident_id"])
        history = list(node.attrs.get("history", [])) if node else []
        history.append({"status": m["status"], "timestamp": m.get("timestamp"), "confidence": m.get("confidence"),
                        "severity": m.get("severity")})
        node = g.put("incident", m["incident_id"], now, corr=m.get("correlation_id"), type=m["type"],
                     zone_id=m["zone_id"], status=m["status"], confidence=m.get("confidence"),
                     severity=m.get("severity"), verified=m.get("verified"), reason=m.get("reason"),
                     evidence=m.get("evidence") or [], history=history[-20:], timestamp=m.get("timestamp"))
        node.attrs.setdefault("opened_at", m.get("timestamp"))
        node.at = now                                  # "recent" = last change
        g.link(node, "IN", g.get("zone", m["zone_id"]))
        if m.get("correlation_id"):
            self._store_reading(m["correlation_id"], "chain start")
            g.link(node, "EVIDENCE", g.get("reading", m["correlation_id"]))
        for e in m.get("evidence") or []:
            if e.get("kind") == "call":
                g.link(node, "EVIDENCE", g.get("call", e["source_id"]))
                continue
            sensor = g.get("sensor", e["source_id"])
            latest = (sensor.attrs.get("latest") or {}) if sensor else {}
            rid = latest.get("reading_id")
            reading = self._store_reading(rid, "evidence") if rid else None
            g.link(node, "EVIDENCE", reading or sensor)

    def on_proposal(self, m: dict[str, Any]) -> None:
        node = self.g.put("proposal", m["id"], self.clock(), corr=m.get("correlation_id"),
                          incident_id=m["incident_id"], origin=m.get("origin"),
                          actions=[f"{a.get('action')} {a.get('target_id')}" for a in m.get("actions") or []],
                          justification=(m.get("justification") or "")[:200], timestamp=m.get("timestamp"))
        self.g.link(node, "FOR", self.g.get("incident", m["incident_id"]))

    def on_decision(self, m: dict[str, Any]) -> None:
        node = self.g.put("decision", m["id"], self.clock(), corr=m.get("correlation_id"),
                          incident_id=m["incident_id"], chosen_proposal=m.get("chosen_proposal"),
                          origin=m.get("origin"), score=m.get("score"), risk=m.get("risk"), mode=m.get("mode"),
                          actions=[f"{a.get('action')} {a.get('target_id')}" for a in m.get("actions") or []],
                          explanation=(m.get("explanation") or "")[:200], timestamp=m.get("timestamp"))
        self.g.link(node, "FOR", self.g.get("incident", m["incident_id"]))
        if m.get("chosen_proposal"):
            self.g.link(node, "CHOSE", self.g.get("proposal", m["chosen_proposal"]))

    def on_outcome(self, m: dict[str, Any]) -> None:
        """One action of a decision; its sent / acknowledged / failed outcomes update the same node."""
        ident = f"{m['decision_id']}/{m['target_id']}/{m.get('action', '')}"
        node = self.g.get("action", ident)
        history = list(node.attrs.get("history", [])) if node else []
        history.append({"status": m["status"], "timestamp": m.get("timestamp"), "outcome_id": m["id"]})
        node = self.g.put("action", ident, self.clock(), corr=m.get("correlation_id"),
                          incident_id=m["incident_id"], decision_id=m["decision_id"], target_id=m["target_id"],
                          action=m.get("action"), status=m["status"], rule=m.get("rule"),
                          detail=(m.get("detail") or "")[:120] or None, history=history[-10:],
                          timestamp=m.get("timestamp"))
        node.at = self.clock()
        if m.get("command_id"):
            node.attrs["command_id"] = m["command_id"]
            self.actions_by_command[m["command_id"]] = ident
            if len(self.actions_by_command) > int(self.cfg["caps"]["action"]) * 2:
                self.actions_by_command.pop(next(iter(self.actions_by_command)))
        self.g.link(node, "EXECUTES", self.g.get("decision", m["decision_id"]))
        self.g.link(node, "TARGETS", self.g.get("actuator", m["target_id"]) or self.g.get("unit", m["target_id"]))

    # --- queries ------------------------------------------------------------------------

    def _neighbours(self, zone_id: str) -> list[str]:
        z = self.g.get("zone", zone_id)
        return sorted(n.id for n in self.g.targets(z, "NEXT_TO")) if z else []

    def _actuators_for(self, zones: list[str]) -> list[dict[str, Any]]:
        out = []
        for own in (True, False):                     # the zone's own actuators first
            for a in self.g.of_kind("actuator"):
                controls = set(a.attrs.get("controls") or []) | {a.attrs.get("zone_id")}
                if (zones[0] in controls) == own and controls & set(zones):
                    out.append({"id": a.id, "type": a.attrs.get("type"), "zone_id": a.attrs.get("zone_id"),
                                "state": a.attrs.get("state"), "quarantined": bool(a.attrs.get("quarantined"))})
        return out

    def _units(self) -> list[dict[str, Any]]:
        return [{"id": u.id, "type": u.attrs.get("type"), "status": u.attrs.get("status"),
                 "zone_id": u.attrs.get("current_zone") or u.attrs.get("home_zone")}
                for u in self.g.of_kind("unit")]

    def _quarantined(self) -> list[str]:
        return sorted(n.id for kind in ("actuator", "unit", "sensor") for n in self.g.of_kind(kind)
                      if n.attrs.get("quarantined"))

    def _related(self, inc: Node, area: set[str]) -> list[dict[str, Any]]:
        now = self.clock()
        window = float(self.cfg["context"]["related_window_s"])
        chars = int(self.cfg["context"]["summary_chars"])
        items: list[tuple[float, dict[str, Any]]] = []
        for o in self.g.of_kind("incident"):
            if o.key != inc.key and o.attrs.get("zone_id") in area and now - o.at <= window:
                items.append((o.at, {"type": o.attrs["type"], "kind": "incident", "zone_id": o.attrs["zone_id"],
                                     "status": o.attrs.get("status"),
                                     "summary": f"{o.id} severity {o.attrs.get('severity')}"}))
        for c in self.g.of_kind("call"):
            if c.attrs.get("zone_id") in area and now - c.at <= window:
                items.append((c.at, {"type": c.attrs.get("category"), "kind": "call", "zone_id": c.attrs["zone_id"],
                                     "summary": (c.attrs.get("text") or "")[:chars]}))
        for a in self.g.of_kind("action"):
            if a.attrs.get("incident_id") == inc.id or now - a.at > window:
                continue
            target = self.g.get("actuator", a.attrs["target_id"]) or self.g.get("unit", a.attrs["target_id"])
            zone = (target.attrs.get("zone_id") or target.attrs.get("current_zone")) if target else None
            if zone in area:
                items.append((a.at, {"type": a.attrs.get("action"), "kind": "action", "zone_id": zone,
                                     "status": a.attrs.get("status"),
                                     "summary": f"{a.attrs.get('action')} {a.attrs['target_id']}"}))
        items.sort(key=lambda x: -x[0])                # newest first
        return [i for _, i in items[: int(self.cfg["context"]["max_related"])]]

    def context(self, incident_id: str) -> Answer:
        """GET /context/{incident_id}: compact JSON under context.max_chars."""
        with self.lock:
            inc = self.g.get("incident", incident_id)
            if inc is None:
                return 404, {"error": "unknown incident", "incident_id": incident_id}
            c = self.cfg["context"]
            zone = inc.attrs["zone_id"]
            neighbours = self._neighbours(zone)
            area = {zone, *neighbours}
            z = self.g.get("zone", zone)
            doc: dict[str, Any] = {
                "incident": {k: inc.attrs.get(k) for k in ("type", "zone_id", "status", "confidence", "severity",
                                                          "verified")} | {"id": inc.id},
                "evidence": [{k: e[k] for k in ("source_id", "kind", "value", "unit", "category", "trust")
                              if e.get(k) is not None} for e in inc.attrs.get("evidence", [])][: int(c["max_evidence"])],
                "zone": {"id": zone, "name": z.attrs.get("name") if z else None, "neighbours": neighbours},
                "actuators": self._actuators_for([zone, *neighbours])[: int(c["max_actuators"])],
                "units": [u for u in self._units() if u["status"] in (None, "free")][: int(c["max_units"])],
                "quarantined": self._quarantined(),
                "related": self._related(inc, area),
            }
            past = self._history_for(inc)
            if past is not None:
                doc["history"] = {k: past[k] for k in ("match", "hint") if k in past}
        return 200, fit(doc, int(c["max_chars"]))

    def _history_for(self, inc: Node) -> dict[str, Any] | None:
        """The Ida reference for this incident (None if the library has nothing comparable)."""
        if not self.history.loaded:
            return None
        now = self.clock()
        calls = sum(1 for n in self.g.of_kind("call")
                    if n.attrs.get("category") == inc.attrs["type"] and now - n.at <= 3600)
        return self.history.match(inc.attrs["type"], inc.attrs.get("severity"), calls)

    def history_view(self, incident_id: str | None = None) -> Answer:
        """GET /history: the reference library. GET /history/{incident_id}: its match for one incident."""
        if not self.history.loaded:
            return 404, {"error": "no incident history loaded (graph.yaml history.enabled / file)"}
        if incident_id is None:
            return 200, self.history.summary()
        with self.lock:
            inc = self.g.get("incident", incident_id)
            if inc is None:
                return 404, {"error": "unknown incident", "incident_id": incident_id}
            past = self._history_for(inc)
            head = {"incident_id": incident_id, "type": inc.attrs["type"], "severity": inc.attrs.get("severity")}
            level = self.history.level_for(inc.attrs.get("severity"))
        if past is None:
            return 200, head | {"level": level, "match": None,
                                "why": "no comparable incident type or level in the history"}
        return 200, head | past

    def zone(self, zone_id: str) -> Answer:
        with self.lock:
            z = self.g.get("zone", zone_id)
            if z is None:
                return 404, {"error": "unknown zone", "zone_id": zone_id}
            sensors = [{"id": s.id, "type": s.attrs.get("type"), "unit": s.attrs.get("unit"),
                        "value": (s.attrs.get("latest") or {}).get("value"),
                        "timestamp": (s.attrs.get("latest") or {}).get("timestamp"),
                        "trust": s.attrs.get("trust"), "quarantined": bool(s.attrs.get("quarantined"))}
                       for s in self.g.sources(z, "IN", "sensor")]
            units = [u for u in self._units() if u["zone_id"] == zone_id]
            incidents = [{"id": i.id, "type": i.attrs["type"], "status": i.attrs["status"],
                          "severity": i.attrs.get("severity")}
                         for i in self.g.of_kind("incident")
                         if i.attrs.get("zone_id") == zone_id and i.attrs.get("status") != "closed"]
            return 200, {"zone_id": zone_id, "name": z.attrs.get("name"), "type": z.attrs.get("type"),
                         "exposure": z.attrs.get("exposure"), "neighbours": self._neighbours(zone_id),
                         "sensors": sorted(sensors, key=lambda s: s["id"]),
                         "actuators": self._actuators_for([zone_id]),
                         "units": units, "open_incidents": incidents}

    def actuators(self, zone_id: str | None) -> Answer:
        with self.lock:
            if zone_id is None:
                items = [{"id": a.id, "type": a.attrs.get("type"), "zone_id": a.attrs.get("zone_id"),
                          "controls": a.attrs.get("controls"), "state": a.attrs.get("state"),
                          "quarantined": bool(a.attrs.get("quarantined"))} for a in self.g.of_kind("actuator")]
                return 200, {"actuators": items}
            if self.g.get("zone", zone_id) is None:
                return 404, {"error": "unknown zone", "zone_id": zone_id}
            own = [a for a in self.g.of_kind("actuator") if zone_id in (a.attrs.get("controls") or [])]
            items = [{"id": a.id, "type": a.attrs.get("type"), "zone_id": a.attrs.get("zone_id"),
                      "actions": a.attrs.get("actions"), "state": a.attrs.get("state"),
                      "quarantined": bool(a.attrs.get("quarantined"))} for a in own]
            return 200, {"zone_id": zone_id, "actuators": items}

    def chain(self, corr: str) -> Answer:
        """GET /chain/{correlation_id}: reading -> verdict -> incident -> proposal -> decision -> action -> ack."""
        with self.lock:
            nodes = [n for n in self.g.chain(corr) if n.kind in STAGES]
            if not nodes:
                return 404, {"error": "unknown correlation_id", "correlation_id": corr}
            nodes.sort(key=lambda n: (STAGES[n.kind], n.attrs.get("timestamp") or "", n.at))
            items = [_chain_item(n) for n in nodes][: int(self.cfg["chain"]["max_items"])]
        return 200, {"correlation_id": corr, "steps": items}

    def view(self) -> Answer:
        """GET /graph: the city and the most recent incidents with what is linked to them."""
        with self.lock:
            keep = {n.key for kind in CITY_KINDS for n in self.g.of_kind(kind)}
            recent = sorted(self.g.of_kind("incident"), key=lambda n: -n.at)[: int(self.cfg["graph_view"]["max_incidents"])]
            for inc in recent:
                keep.add(inc.key)
                for n in self.g.targets(inc) + self.g.sources(inc):
                    keep.add(n.key)
                    for m in self.g.sources(n):          # actions of a decision, verdicts of a reading
                        if m.kind not in CITY_KINDS:
                            keep.add(m.key)
                            for a in self.g.sources(m, "CONFIRMS"):
                                keep.add(a.key)
            nodes = [{"id": self.g.nodes[k].key, "kind": self.g.nodes[k].kind, "label": _label(self.g.nodes[k])}
                     for k in keep]
            edges = [{"source": s, "target": d, "rel": r} for s, r, d in self.g.edges() if s in keep and d in keep]
        return 200, {"nodes": nodes, "edges": edges}

    def health(self) -> Answer:
        with self.lock:
            counts = {k: self.g.count(k) for k in sorted(self.g.by_kind)}
            return 200, {"status": "ok", "meta_loaded": self.g.count("zone") > 0, "nodes": counts,
                         "history": self.history.lib.get("source") if self.history.loaded else None,
                         "edges": sum(len(v) for v in self.g.out.values()), "dropped": self.g.dropped,
                         "messages": self.messages, "layers": self.layers,
                         "uptime_s": round(self.clock() - self.started, 1)}


# --- helpers -------------------------------------------------------------------------------


def dumps(doc: Any) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def fit(doc: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Trim the lowest-value parts until the compact JSON fits in max_chars.

    Related events go down to 3 and free units to 2 (the LLM takes its devices from its own city
    state), then the history hint (reference data from another city) is dropped whole, then the
    remaining lists are trimmed as before."""
    order = (("related", 3), ("units", 2), ("history", None), ("related", 0), ("units", 0), ("actuators", 0),
             ("evidence", 0), ("quarantined", 0))
    while len(dumps(doc)) > max_chars:
        for name, keep in order:
            if keep is None:
                if doc.get(name):
                    del doc[name]
                    doc["trimmed"] = True
                    break
            elif len(doc.get(name) or []) > keep:
                doc[name].pop()
                doc["trimmed"] = True
                break
        else:
            break
    return doc


_iso = iso_utc


def _chain_item(n: Node) -> dict[str, Any]:
    a = n.attrs
    item: dict[str, Any] = {"stage": n.kind, "id": n.id, "timestamp": a.get("timestamp") or _iso(n.at)}
    if n.kind == "reading":
        item["summary"] = f"{a['sensor_id']} = {a['value']} {a.get('unit') or ''}".strip()
    elif n.kind == "verdict":
        item["summary"] = f"{a['sensor_id']} {a['verdict']}" + (f" ({a['reason']})" if a.get("reason") else "")
    elif n.kind == "call":
        item["summary"] = f"{a.get('category')} call in {a.get('zone_id')}"
    elif n.kind == "incident":
        item["summary"] = (f"{a['type']} at {a['zone_id']} {a['status']} "
                           f"(confidence {a.get('confidence')}, {a.get('severity')})")
        item["history"] = [h["status"] for h in a.get("history", [])]
    elif n.kind == "proposal":
        item["summary"] = f"{a.get('origin')}: " + ", ".join(a.get("actions") or [])
    elif n.kind == "decision":
        item["summary"] = f"chose {a.get('chosen_proposal')} ({a.get('origin')}): " + ", ".join(a.get("actions") or [])
    elif n.kind == "action":
        item["summary"] = f"{a.get('action')} {a['target_id']}: {a['status']}" + (
            f" ({a['rule']})" if a.get("rule") else "")
        item["history"] = [h["status"] for h in a.get("history", [])]
        if a.get("command_id"):
            item["command_id"] = a["command_id"]
    elif n.kind == "ack":
        item["summary"] = f"{a['target_id']} {a['result']}" + (f" ({a['state']})" if a.get("state") else "")
    return item


def _label(n: Node) -> str:
    a = n.attrs
    if n.kind == "zone":
        return f"{n.id} {a.get('name') or ''}".strip()
    if n.kind == "incident":
        return f"{a.get('type')} {n.id} ({a.get('status')})"
    if n.kind == "reading":
        return f"{a.get('sensor_id')}={a.get('value')}"
    if n.kind == "action":
        return f"{a.get('action')} {a.get('target_id')} ({a.get('status')})"
    return n.id
