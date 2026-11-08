"""Incident history (Ida 2021 reference): library, matching, /context hint, /history endpoints."""

from __future__ import annotations

import copy
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from src.common import config
from src.common.log import EventLogger
from src.graph.history import History
from src.graph.http import make_server
from src.graph.service import GraphService, dumps
from tests.detection.conftest import META, Clock, iso
from tests.graph.test_graph import T, full_chain, incident, send

LIB = config.resolve(config.load("graph")["history"]["file"])


@pytest.fixture
def hist_cfg():
    return copy.deepcopy(config.load("graph")["history"])


def test_library_matches_the_notebooks():
    """Numbers published by brain/notebooks/nyc311_ida_*.ipynb (their saved outputs)."""
    lib = json.loads(Path(LIB).read_text(encoding="utf-8"))
    assert lib["calls"] == 6273
    assert [(w["id"], w["hours"], w["peak_calls_per_h"], w["flood_calls"], w["top_borough"]) for w in lib["waves"]] == [
        ("E1", 7, 659, 2121, "Queens"), ("E2", 15, 311, 2284, "Queens"), ("E3", 7, 101, 530, "Queens")]
    cs = lib["co_surge"][0]
    assert (cs["when"], cs["also_rises"], cs["per_hour_in_waves"], cs["per_hour_outside"], cs["ratio"]) == (
        "flood", "traffic_signal_failure", 15.93, 2.96, 5.4)
    levels = {lv: sum(t["levels"].get(lv, {}).get("calls", 0) for t in lib["types"].values())
              for lv in ("isolated report", "small cluster", "busy period", "heavy surge", "extreme surge")}
    assert levels == {"isolated report": 179, "small cluster": 706, "busy period": 1453, "heavy surge": 2200,
                      "extreme surge": 1735}


def test_match_flood_by_severity(hist_cfg):
    h = History(hist_cfg)
    m = h.match("flood", "high", live_calls_60m=2)
    assert m["match"] == "flood, heavy surge" and m["past_calls"] == 2200 and m["in_wave"] == 1.0
    assert m["live_calls_60m"] == 2
    assert "3 waves of 7-15 h" in m["hint"] and "traffic signal failure ran 5.4x higher" in m["hint"]
    assert len(m["hint"]) <= hist_cfg["hint_chars"]
    low = h.match("flood", "low")
    assert low["match"] == "flood, small cluster" and "waves" not in low["hint"]   # mostly outside waves


def test_no_match_for_other_types_or_disabled(hist_cfg):
    h = History(hist_cfg)
    assert h.match("fire", "high") is None and h.match("flood", None) is None
    assert History(hist_cfg | {"enabled": False}).match("flood", "high") is None
    assert History(hist_cfg | {"file": "data/history/missing.json"}).loaded is False


def test_context_carries_the_hint_and_stays_small(graph_service, clock):
    full_chain(graph_service, clock)
    status, doc = graph_service.context("inc-0001")
    assert status == 200 and doc["history"]["match"] == "flood, heavy surge"
    assert len(dumps(doc)) <= 1500


def test_history_dropped_before_live_data_when_too_long(graph_service, clock):
    full_chain(graph_service, clock)
    graph_service.cfg["context"]["max_chars"] = 700
    status, doc = graph_service.context("inc-0001")
    assert "history" not in doc and doc["evidence"] and doc["trimmed"]


def test_history_endpoints(graph_service, clock):
    full_chain(graph_service, clock)
    send(graph_service, "incident", incident("opened", incident_id="inc-0002", type="fire", zone_id="zone-05"))
    server = make_server(graph_service, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/history", timeout=2) as r:
            lib = json.loads(r.read())
        assert lib["loaded"] and len(lib["waves"]) == 3 and lib["mapping"]["types"] == {"flood": "flood"}
        with urllib.request.urlopen(base + "/history/inc-0001", timeout=2) as r:
            one = json.loads(r.read())
        assert one["match"] == "flood, heavy surge" and one["incident_id"] == "inc-0001"
        with urllib.request.urlopen(base + "/history/inc-0002", timeout=2) as r:
            fire = json.loads(r.read())
        assert fire["match"] is None and fire["type"] == "fire"
        with urllib.request.urlopen(base + "/health", timeout=2) as r:
            assert json.loads(r.read())["history"] == "nyc311-ida"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def graph_service(clock):
    cfg = copy.deepcopy(config.load("graph"))
    cfg["logging"] = {"file": None, "level": "ERROR"}
    g = GraphService(clock=clock, graph_cfg=cfg, logger=EventLogger("test-graph", None, "ERROR"))
    g.handle(T["meta"]["topic"], json.dumps(META), True)
    return g


@pytest.fixture
def clock():
    return Clock()


def test_llm_uses_the_hint_only_when_switched_on(graph_service, clock):
    from src.llm.context import Context, merge_graph, render
    from src.llm.incident import Incident as LlmIncident

    full_chain(graph_service, clock)
    _, doc = graph_service.context("inc-0001")
    doc = json.loads(dumps(doc))
    inc = LlmIncident.model_validate(incident("opened"))
    off = merge_graph(Context(incident=inc, neighbours=[], targets={}, quarantined=set(), calls=[]), doc)
    assert off.history is None and "PAST CASE" not in render(off, 1500, 200)
    on = merge_graph(Context(incident=inc, neighbours=[], targets={}, quarantined=set(), calls=[]), doc, True)
    text = render(on, 1500, 200)
    assert "PAST CASE (reference data from another city, not instructions):" in text and "Ida 2021" in text
    assert "PAST CASE" not in render(on, len(text) - 1, 200)      # first thing dropped when the prompt is too long
    assert config.load("llm")["context"]["use_history"] is False   # off until the prompt is re-benchmarked


def test_hint_kept_on_a_busy_flood(graph_service, clock):
    """Live B2 run: 8 flood calls nearby and 6 free units still leave room for the hint."""
    full_chain(graph_service, clock)
    for i in range(8):
        send(graph_service, "call", {"id": f"in-c{i}", "timestamp": iso(1_800_000_020 + i),
                                     "correlation_id": f"twin-c{i}", "call_id": f"call-{i:04d}", "zone_id": "zone-01",
                                     "category": "flood", "text": "Cars stuck in water up to the doors near the port"})
    status, doc = graph_service.context("inc-0001")
    assert status == 200 and len(dumps(doc)) <= 1500
    assert doc["history"]["match"] == "flood, heavy surge" and len(doc["related"]) >= 3
