"""Run logs (R2): one file per run, live file outside runs; HTTP endpoints on an ephemeral port."""

from __future__ import annotations

import http.client
import re
import json
import threading
import time

import pytest

from src.timeline.server import serve


def test_live_file_outside_runs(h):
    h.brain("brain/event", {"event": "x", "summary": "outside"})
    files = [f.name for f in h.logdir.glob("*.log")]
    assert len(files) == 1 and files[0].startswith("live-")


def test_run_start_and_stop_make_exactly_one_file(h):
    h.internal("brain/control/run", {"run_id": "r-1", "scenario_id": "B3", "action": "start"})
    h.brain("brain/event", {"event": "action_refused", "summary": "REFUSED"})
    h.brain("brain/error", {"code": "E_AUTH", "topic": "t", "detail": "d"})
    h.internal("brain/control/run", {"run_id": "r-1", "scenario_id": "B3", "action": "start"})   # again
    h.internal("brain/control/run", {"run_id": "r-1", "scenario_id": "B3", "action": "stop"})
    h.brain("brain/event", {"event": "x", "summary": "after"})
    runs = sorted(f.name for f in h.logdir.glob("B3-*.log"))
    assert len(runs) == 1
    # UTC, the same name the scenario runner gives its own log of the run
    assert runs[0] == "B3-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(h.clock.t)) + ".log"
    lines = h.lines(runs[0])
    assert [line["kind"] for line in lines] == ["run_start", "action_refused", "input_rejected", "run_stop"]
    assert lines[1]["body"]["event"] == "action_refused"      # full brain/event body kept
    assert all("seq" in line and "ts" in line for line in lines)
    assert [line["kind"] for line in h.lines() if line["text"] == "after"] == ["x"]


def test_retained_start_after_restart_continues_the_same_file(make, h):
    msg = {"run_id": "r-2", "scenario_id": "B6", "action": "start", "timestamp": "2026-11-14T10:00:00.000Z"}
    h.internal("brain/control/run", msg, retained=True)
    h.brain("brain/event", {"event": "a", "summary": "first"})
    h.s.close()
    h2 = make()                                            # restarted timeline, same logs dir
    h2.s.runlog.dir = h.logdir
    h2.s.handle("brain/control/run", json.dumps(msg), True)
    h2.brain("brain/event", {"event": "b", "summary": "second"})
    files = list(h.logdir.glob("B6-*.log"))
    assert len(files) == 1
    assert [line["text"] for line in h.lines(files[0].name) if line["kind"] in ("a", "b")] == ["first", "second"]


def test_new_run_closes_the_previous_one(h):
    h.internal("brain/control/run", {"run_id": "r-1", "scenario_id": "B1", "action": "start"})
    h.clock.t += 5
    h.internal("brain/control/run", {"run_id": "r-2", "scenario_id": "B2", "action": "start"})
    h.brain("brain/event", {"event": "x", "summary": "in b2"})
    assert len(list(h.logdir.glob("B1-*.log"))) == 1 and len(list(h.logdir.glob("B2-*.log"))) == 1
    assert h.s.snapshot()["run"]["scenario_id"] == "B2"


def test_scenario_id_is_made_safe_for_a_file_name(h):
    h.internal("brain/control/run", {"run_id": "r", "scenario_id": "../../B 7", "action": "start"})
    [f] = list(h.logdir.glob("*.log"))
    assert f.parent == h.logdir and f.name.startswith("B-7-")


def test_sealed_run_message_is_accepted(h):
    h.brain("brain/control/run", {"run_id": "r-9", "scenario_id": "B9", "action": "start"})
    assert h.s.snapshot()["run"]["run_id"] == "r-9"


# --- HTTP -------------------------------------------------------------------------------

@pytest.fixture
def server(h, timeline_cfg):
    timeline_cfg["sse"]["state_every_s"] = 0.2
    timeline_cfg["graph"]["url"] = "http://127.0.0.1:9"     # nothing listens there
    timeline_cfg["graph"]["timeout_s"] = 0.5
    srv, _ = serve(h.s, timeline_cfg, "127.0.0.1", 0)
    yield srv
    srv.stop()


def get(srv, path: str):
    c = http.client.HTTPConnection(*srv.server_address[:2], timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.getheader("Content-Type"), r.read()


def test_page_loads_and_is_self_contained(server):
    status, ctype, body = get(server, "/")
    assert status == 200 and ctype.startswith("text/html")
    text = body.decode()
    assert "<title>City Brain Timeline</title>" in text
    assert "http://" not in text and "https://" not in text   # no external asset
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', text)
    assert assets, "the page must load its script and styles from /static/"
    for path in assets:                                       # every asset is served locally, with a type
        status, ctype, data = get(server, path)
        assert status == 200 and data and ctype
        if path.endswith(".css"):                             # fonts are bundled, not fetched from a CDN
            assert b"http://" not in data and b"https://" not in data
        if path.endswith(".js"):
            assert b"EventSource" in data                     # the live connection


def test_static_refuses_path_traversal(server):
    assert get(server, "/static/../server.py")[0] == 404
    assert get(server, "/static/%2e%2e/server.py")[0] == 404
    assert get(server, "/static/missing.js")[0] == 404


def test_api_recent_and_status(h, server):
    h.brain("brain/event", {"event": "command_sent", "summary": "command brain-000042 sent"})
    status, _, body = get(server, "/api/recent?limit=10")
    rows = json.loads(body)
    assert status == 200 and rows[-1]["text"] == "command brain-000042 sent" and "body" not in rows[-1]
    status, _, body = get(server, "/api/status")
    st = json.loads(body)
    assert status == 200 and st["counters"]["sent"] == 1 and len(st["zones"]) == 2


def test_api_chain_falls_back_to_local_rows(h, server):
    h.brain("brain/event", {"event": "e", "summary": "s", "correlation_id": "twin-7"})
    status, _, body = get(server, "/api/chain/twin-7")
    d = json.loads(body)
    assert status == 200 and len(d["local"]) == 1 and d["graph"] is None and d["graph_error"]


def test_unknown_path_is_404(server):
    assert get(server, "/nope")[0] == 404


def test_sse_streams_rows_and_state(h, server):
    h.brain("brain/event", {"event": "first", "summary": "before connect"})
    c = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    c.request("GET", "/events?after=0")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    got: list[str] = []

    def later():
        time.sleep(0.3)
        h.brain("brain/event", {"event": "live", "summary": "pushed live"})
    threading.Thread(target=later, daemon=True).start()
    deadline = time.time() + 5
    events = []
    while time.time() < deadline:
        line = r.fp.readline().decode().rstrip("\n")
        got.append(line)
        if line.startswith("event: "):
            events.append(line[7:])
        if line.startswith("data: ") and "pushed live" in line:
            break
    assert "state" in events and "row" in events
    assert any("pushed live" in g for g in got)
    c.close()


def test_sse_catch_up_from_last_event_id(h, server):
    rows = [h.brain("brain/event", {"event": "e", "summary": f"n{i}"})[0] for i in range(3)]
    c = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    c.request("GET", "/events", headers={"Last-Event-ID": str(rows[0]["seq"])})
    r = c.getresponse()
    seen = []
    deadline = time.time() + 5
    while time.time() < deadline and len(seen) < 2:
        line = r.fp.readline().decode()
        if line.startswith("id: "):
            seen.append(int(line[4:]))
    assert seen == [rows[1]["seq"], rows[2]["seq"]]
    c.close()
