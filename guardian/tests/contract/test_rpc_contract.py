"""Guardian vs mocks/: JSON-RPC 2.0 contract of the 4 tools, as consumed by brain_mock."""
from src.core.events import VerdictSigner
from src.core.security import issue_tool_token
from tests.conftest import warm


def raw(stack, body, role="brain", tool=None):
    h = {}
    if role:
        h["Authorization"] = "Bearer " + issue_tool_token(stack.keys[role], role, tool or body.get("method"))
    return stack.client.post("/rpc", json=body, headers=h).json()


def test_get_trust_verdict_shape_and_signature(stack):
    warm(stack)
    eid = stack.verdicts[-1]["event_id"]
    r = stack.rpc("brain").call("get_trust_verdict", {"event_id": eid})
    assert set(r) == {"event_id", "trust_score", "verdict", "reason", "sig"}
    assert r["event_id"] == eid and r["verdict"] in ("trusted", "suspicious", "rejected")
    assert 0.0 <= r["trust_score"] <= 1.0
    assert VerdictSigner.verify(r, stack.client.get("/.well-known/guardian-verdict-key").text)


def test_get_device_status_values(stack):
    warm(stack)
    assert stack.rpc("twin").call("get_device_status", {"device_id": "wl-1"}) == {"status": "trusted"}
    assert stack.rpc("twin").call("get_device_status", {"device_id": "unknown"}) == {"status": "trusted"}


def test_release_device_and_health(stack):
    assert stack.rpc("admin").call("release_device", {"device_id": "x"})["status"] in (
        "trusted", "suspicious", "quarantined")
    assert stack.rpc("brain").call("health") == {"status": "ok"}


def test_jsonrpc_errors(stack):
    r = raw(stack, {"jsonrpc": "2.0", "method": "nope", "id": 1}, tool="nope")
    assert r["error"]["code"] == -32601 and r["id"] == 1
    r = raw(stack, {"jsonrpc": "2.0", "method": "get_trust_verdict", "params": {}, "id": 2})
    assert r["error"]["code"] == -32602
    r = raw(stack, {"jsonrpc": "2.0", "method": "get_trust_verdict", "params": {"event_id": "zz"}, "id": 3})
    assert r["error"]["code"] == -32004
    r = raw(stack, {"method": "health", "id": 4})
    assert r["error"]["code"] == -32600
    r = raw(stack, {"jsonrpc": "2.0", "method": "health", "id": 5}, role=None)
    assert r["error"]["code"] == -32001
    r = raw(stack, {"jsonrpc": "2.0", "method": "release_device", "params": {"device_id": "x"}, "id": 6})
    assert r["error"]["code"] == -32003
    assert stack.client.post("/rpc", content=b"{bad").json()["error"]["code"] == -32700


def test_batch_requires_token_per_call(stack):
    k = stack.keys["brain"]
    body = [{"jsonrpc": "2.0", "method": "health", "id": 1,
             "params": {"_jwt": issue_tool_token(k, "brain", "health")}},
            {"jsonrpc": "2.0", "method": "health", "id": 2}]
    out = stack.client.post("/rpc", json=body).json()
    assert out[0]["result"] == {"status": "ok"} and out[1]["error"]["code"] == -32001


def test_websocket_transport(stack):
    with stack.client.websocket_connect("/rpc/ws") as ws:
        ws.send_json({"jsonrpc": "2.0", "method": "health", "id": 9,
                      "params": {"_jwt": issue_tool_token(stack.keys["ui"], "ui", "health")}})
        assert ws.receive_json() == {"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 9}


def test_timeline_endpoint(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
    tok = issue_tool_token(stack.keys["ui"], "ui", "timeline")
    r = stack.client.get("/timeline", headers={"Authorization": f"Bearer {tok}"}).json()
    kinds = [e["kind"] for e in r["entries"]]
    assert r["layer"] == "guardian" and "quarantine" in kinds and "verdict_rejected" in kinds
    assert stack.published("triplet/timeline/guardian")
    assert stack.client.get("/timeline").status_code == 401


def test_brain_mock_contract(stack):
    warm(stack)
    e = stack.twin.normal("wl-1", base=1.2)
    assert stack.brain.on_twin_event(e)["verdict"] == "trusted"


def test_twin_mock_quarantine_contract(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
    acks = stack.published("twin/acks/quarantine/wl-1")
    assert acks and acks[-1][1]["state"] == "quarantined"
    r = stack.twin.execute_command("wl-1", "open_floodgate")
    assert r == {"accepted": False, "error": {"code": "DEVICE_QUARANTINED",
                                              "message": "wl-1 is quarantined by Guardian"}}
