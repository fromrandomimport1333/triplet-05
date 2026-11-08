"""Graph: city from meta, events linked by correlation_id, context API (02-graph section 5)."""

from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from src.common import config
from src.common.log import EventLogger
from src.graph.http import make_server
from src.graph.service import GraphService, dumps
from src.llm.context import Context, merge_graph
from src.llm.incident import Incident as LlmIncident
from tests.detection.conftest import META, Bus, City, Clock, iso
from src.detection.service import DetectionService

T = config.load("topics")["internal"]


@pytest.fixture
def graph_cfg():
    cfg = copy.deepcopy(config.load("graph"))
    cfg["logging"] = {"file": None, "level": "ERROR"}
    return cfg


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def graph(graph_cfg, clock):
    g = GraphService(clock=clock, graph_cfg=graph_cfg, logger=EventLogger("test-graph", None, "ERROR"))
    g.handle(T["meta"]["topic"], json.dumps(META), True)
    return g


def send(g: GraphService, name: str, msg: dict, **fmt) -> None:
    g.handle(T[name]["topic"].format(**fmt) if fmt else T[name]["topic"], json.dumps(msg))


def reading(n: int, sensor: str, zone: str, stype: str, value: float, unit: str = "m") -> dict:
    return {"id": f"in-r{n}", "timestamp": iso(1_800_000_000 + n), "correlation_id": f"twin-{n}",
            "sensor_id": sensor, "zone_id": zone, "type": stype, "value": value, "unit": unit}


def incident(status: str, corr: str = "twin-2", conf: float = 0.9, **kw) -> dict:
    d = {"id": f"det-{status}", "timestamp": iso(1_800_000_010), "correlation_id": corr, "incident_id": "inc-0001",
         "type": "flood", "zone_id": "zone-01", "status": status, "confidence": conf, "severity": "high",
         "verified": True, "evidence": [{"source_id": "water-01", "kind": "reading", "value": 2.1, "unit": "m",
                                         "trust": 1.0}], "reason": "water-01 at alert level"}
    d.update(kw)
    return d


def full_chain(g: GraphService, clock: Clock) -> None:
    """B2: reading -> verdict -> incident -> proposals -> decision -> command -> ack."""
    send(g, "reading", reading(1, "water-01", "zone-01", "water_level", 0.41))
    send(g, "reading", reading(2, "water-01", "zone-01", "water_level", 1.6))
    send(g, "trust", {"id": "in-v2", "timestamp": iso(1_800_000_002), "correlation_id": "twin-2",
                      "sensor_id": "water-01", "verdict": "trusted", "trust_score": None, "reason": "ok"},
         sensor_id="water-01")
    clock.t += 1
    send(g, "incident", incident("opened"))
    send(g, "proposal", {"id": "llm-000001", "timestamp": iso(1_800_000_011), "correlation_id": "twin-2",
                         "incident_id": "inc-0001", "origin": "llm",
                         "actions": [{"target_id": "valve-01", "action": "open", "params": {}}],
                         "justification": "drain"})
    send(g, "proposal", {"id": "rules-000001", "timestamp": iso(1_800_000_011), "correlation_id": "twin-2",
                         "incident_id": "inc-0001", "origin": "rules",
                         "actions": [{"target_id": "valve-01", "action": "open", "params": {}}],
                         "justification": "standard"})
    send(g, "decision", {"id": "math-000001", "timestamp": iso(1_800_000_012), "correlation_id": "twin-2",
                         "incident_id": "inc-0001", "chosen_proposal": "llm-000001", "origin": "llm",
                         "actions": [{"target_id": "valve-01", "action": "open", "params": {}}], "score": 1.5,
                         "risk": "high", "mode": "normal", "explanation": "open valve-01"})
    send(g, "outcome", {"id": "out-000001", "timestamp": iso(1_800_000_013), "correlation_id": "twin-2",
                        "incident_id": "inc-0001", "decision_id": "math-000001", "target_id": "valve-01",
                        "action": "open", "status": "sent", "command_id": "brain-000042"})
    send(g, "ack", {"id": "in-a1", "timestamp": iso(1_800_000_014), "correlation_id": "brain-000042",
                    "message_id": "twin-99", "kind": "actuator", "target_id": "valve-01", "result": "done",
                    "state": "open"})
    send(g, "outcome", {"id": "out-000002", "timestamp": iso(1_800_000_014), "correlation_id": "twin-2",
                        "incident_id": "inc-0001", "decision_id": "math-000001", "target_id": "valve-01",
                        "action": "open", "status": "acknowledged", "command_id": "brain-000042"})


def test_city_graph_built_from_meta(graph):
    g = graph.g
    assert g.count("zone") == 6 and g.count("sensor") == 44 and g.count("actuator") == 12 and g.count("unit") == 6
    z3 = g.get("zone", "zone-03")
    assert {n.id for n in g.targets(z3, "NEXT_TO")} == {"zone-02", "zone-04", "zone-05", "zone-06"}
    grid = g.get("actuator", "grid-01")
    assert {n.id for n in g.targets(grid, "CONTROLS")} == {"zone-05", "zone-06"}
    assert g.targets(g.get("sensor", "water-01"), "IN")[0].id == "zone-01"
    assert g.targets(g.get("unit", "unit-fire-01"), "BASED_IN")[0].id == "zone-05"


def test_normal_readings_not_stored_alert_ones_are(graph):
    for n in range(1, 50):
        send(graph, "reading", reading(n, "water-01", "zone-01", "water_level", 0.4))
    assert graph.g.count("reading") == 0
    assert graph.g.get("sensor", "water-01").attrs["latest"]["value"] == 0.4
    send(graph, "reading", reading(60, "water-01", "zone-01", "water_level", 1.5))
    assert graph.g.count("reading") == 1


def test_rejected_reading_and_verdict_stored_and_linked(graph):
    send(graph, "reading", reading(5, "water-02", "zone-02", "water_level", 0.9))
    send(graph, "trust", {"id": "in-v5", "timestamp": iso(1_800_000_005), "correlation_id": "twin-5",
                          "sensor_id": "water-02", "verdict": "rejected", "trust_score": 0.1, "reason": "falsified"},
         sensor_id="water-02")
    r = graph.g.get("reading", "twin-5")
    assert r is not None
    v = graph.g.sources(r, "ON", "verdict")
    assert v and v[0].attrs["verdict"] == "rejected"


def test_events_linked(graph, clock):
    full_chain(graph, clock)
    g = graph.g
    inc = g.get("incident", "inc-0001")
    assert {n.id for n in g.targets(inc, "IN")} == {"zone-01"}
    assert {n.kind for n in g.targets(inc, "EVIDENCE")} == {"reading"}
    dec = g.get("decision", "math-000001")
    assert g.targets(dec, "FOR")[0].id == "inc-0001" and g.targets(dec, "CHOSE")[0].id == "llm-000001"
    action = g.sources(dec, "EXECUTES")[0]
    assert action.attrs["status"] == "acknowledged" and [h["status"] for h in action.attrs["history"]] == [
        "sent", "acknowledged"]
    assert g.sources(action, "CONFIRMS")[0].kind == "ack"
    assert g.get("actuator", "valve-01").attrs["state"] == "open"


def test_chain_order(graph, clock):
    full_chain(graph, clock)
    status, doc = graph.chain("twin-2")
    assert status == 200
    stages = [s["stage"] for s in doc["steps"]]
    assert stages == ["reading", "verdict", "incident", "proposal", "proposal", "decision", "action", "ack"]
    assert doc["steps"][-2]["command_id"] == "brain-000042"


def test_context_contents_and_size(graph, clock):
    full_chain(graph, clock)
    for i in range(40):                            # lots of related events nearby
        send(graph, "call", {"id": f"in-c{i}", "timestamp": iso(1_800_000_020 + i), "correlation_id": f"twin-c{i}",
                             "call_id": f"call-{i:04d}", "zone_id": "zone-02", "category": "flood",
                             "text": "water everywhere near the docks " * 5})
    send(graph, "quarantine", {"id": "in-q1", "timestamp": iso(1_800_000_030), "correlation_id": "twin-q",
                               "device_id": "valve-02", "state": "quarantined", "reason": "inconsistent"},
         device_id="valve-02")
    status, doc = graph.context("inc-0001")
    assert status == 200
    assert len(dumps(doc)) < 1500
    assert doc["incident"]["id"] == "inc-0001" and doc["evidence"][0]["source_id"] == "water-01"
    assert doc["zone"]["neighbours"] == ["zone-02"]
    assert doc["actuators"][0]["id"] in ("valve-01", "signal-01")
    assert "valve-02" in doc["quarantined"]
    assert doc["related"] and all({"type", "kind", "zone_id"} <= set(r) for r in doc["related"])
    assert any(a["id"] == "valve-02" and a["quarantined"] for a in doc["actuators"])


def test_context_is_readable_by_the_llm_module(graph, clock):
    full_chain(graph, clock)
    send(graph, "quarantine", {"id": "in-q1", "timestamp": iso(1_800_000_030), "correlation_id": "twin-q",
                               "device_id": "valve-01", "state": "quarantined", "reason": "inconsistent"},
         device_id="valve-01")
    send(graph, "call", {"id": "in-c1", "timestamp": iso(1_800_000_020), "correlation_id": "twin-c1",
                         "call_id": "call-0001", "zone_id": "zone-01", "category": "flood", "text": "help"})
    _, doc = graph.context("inc-0001")
    inc = LlmIncident.model_validate(incident("opened"))
    ctx = Context(incident=inc, neighbours=[], targets={}, quarantined=set(), calls=[])
    ctx = merge_graph(ctx, json.loads(dumps(doc)))
    assert "valve-01" in ctx.quarantined and ctx.related and ctx.source == "graph"


def test_zone_and_actuators(graph, clock):
    full_chain(graph, clock)
    status, z = graph.zone("zone-05")
    assert status == 200
    assert {s["id"] for s in z["sensors"]} >= {"water-05", "power-05"}
    assert {a["id"] for a in z["actuators"]} >= {"valve-03", "grid-01", "signal-05"}
    assert any(u["id"] == "unit-fire-01" for u in z["units"])
    status, a = graph.actuators("zone-06")
    assert {x["id"] for x in a["actuators"]} == {"signal-06", "grid-01"}
    status, a = graph.actuators(None)
    assert len(a["actuators"]) == 12
    _, w = graph.zone("zone-01")
    assert w["open_incidents"][0]["id"] == "inc-0001"
    water = next(s for s in w["sensors"] if s["id"] == "water-01")
    assert water["value"] == 1.6 and water["trust"]["verdict"] == "trusted"


def test_unknown_ids_404(graph):
    assert graph.context("inc-9999")[0] == 404
    assert graph.zone("zone-99")[0] == 404
    assert graph.chain("nope")[0] == 404
    assert graph.actuators("zone-99")[0] == 404


def test_bounded_memory(graph_cfg, clock):
    graph_cfg["caps"]["call"] = 10
    g = GraphService(clock=clock, graph_cfg=graph_cfg, logger=EventLogger("test-graph", None, "ERROR"))
    g.handle(T["meta"]["topic"], json.dumps(META), True)
    for i in range(50):
        send(g, "call", {"id": f"in-c{i}", "timestamp": iso(1_800_000_000 + i), "correlation_id": f"twin-c{i}",
                         "call_id": f"call-{i:04d}", "zone_id": "zone-01", "category": "fire"})
    assert g.g.count("call") == 10 and g.g.get("call", "call-0049") and not g.g.get("call", "call-0000")
    assert g.chain("twin-c0")[0] == 404 and g.chain("twin-c49")[0] == 200
    z = g.g.get("zone", "zone-01")
    assert len(g.g.sources(z, "IN", "call")) == 10


def test_graph_view(graph, clock):
    full_chain(graph, clock)
    status, doc = graph.view()
    ids = {n["id"] for n in doc["nodes"]}
    assert status == 200 and "zone:zone-01" in ids and "incident:inc-0001" in ids and "decision:math-000001" in ids
    assert all(e["source"] in ids and e["target"] in ids for e in doc["edges"])


def test_detection_to_graph_end_to_end(graph, clock):
    """The real detection module's incident lands in the graph with its chain start."""
    bus = Bus()
    det = DetectionService(bus, clock=clock, logger=EventLogger("test-det", None, "ERROR"))

    class Both:
        def handle(self, topic, payload, retained=False):
            det.handle(topic, payload, retained)
            graph.handle(topic, payload, retained)

        def tick(self):
            det.tick()

    city = City(Both(), clock)
    city.trust_all()
    city.step(30)
    city.overrides[("zone-01", "water_level")] = lambda n: min(2.5, 1.4 + (n - 31) * 0.15)
    city.step(6)
    for topic, payload, _, _ in bus.sent:
        graph.handle(topic, payload)
    opened = bus.incidents(status="opened")[0]
    status, doc = graph.chain(opened["correlation_id"])
    assert status == 200 and [s["stage"] for s in doc["steps"]][:2] == ["reading", "incident"]
    status, ctx = graph.context(opened["incident_id"])
    assert status == 200 and len(dumps(ctx)) < 1500


def test_http_server_answers_every_endpoint(graph, clock):
    full_chain(graph, clock)
    server = make_server(graph, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in ("/context/inc-0001", "/zone/zone-01", "/actuators?zone_id=zone-01", "/actuators",
                     "/chain/twin-2", "/graph", "/health"):
            started = time.perf_counter()
            with urllib.request.urlopen(base + path, timeout=2) as r:
                assert r.status == 200 and r.headers["Content-Type"] == "application/json"
                json.loads(r.read())
            assert time.perf_counter() - started < 0.1, path
        for path in ("/context/inc-9999", "/zone/zone-99", "/chain/x", "/nothing"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path, timeout=2)
            assert e.value.code == 404 and "error" in json.loads(e.value.read())
    finally:
        server.shutdown()
        server.server_close()


def test_alert_level_respects_the_sensor_direction():
    """traffic_speed alerts when LOW: a normal 50 km/h must not be stored as an alert reading."""
    from src.graph.service import GraphService
    svc = GraphService()
    class S:
        attrs = {"type": "traffic_speed", "alert": 10}
    assert not svc._at_alert(S, 50) and svc._at_alert(S, 5)
    S.attrs = {"type": "water_level", "alert": 1.3}
    assert svc._at_alert(S, 1.5) and not svc._at_alert(S, 0.4)
