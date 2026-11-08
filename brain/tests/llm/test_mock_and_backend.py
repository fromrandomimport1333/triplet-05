from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mocks.mock_llm.backend import MockBackend
from src.llm.backends import BackendTimeout, BackendUnavailable, OllamaBackend
from src.llm.bench import bench_model, load_cases


PROPOSAL = "brain/internal/proposal"


# --- mock LLM (03-llm section 6) ------------------------------------------------------


def mock(llm_cfg, mode, **kw):
    return MockBackend(llm_cfg["mock"], mode, **kw)


@pytest.mark.parametrize("case_id, expected", [
    ("B2", [("valve-port-up", "open"), ("signal-port-2", "close_inbound")]),
    ("B5", [("ambulance-1", "dispatch"), ("signal-centre-1", "divert")]),
    ("B8", [("valve-port-main", "open"), ("signal-port-2", "close_inbound")]),
    ("B11", [("fire-1", "dispatch"), ("signal-centre-1", "green_corridor")]),
])
def test_scripted_mode_follows_the_playbook(run_case, bus, llm_cfg, case_id, expected):
    _, outcome = run_case(case_id, mock(llm_cfg, "scripted"))
    assert outcome == "proposal"
    assert [(a["target_id"], a["action"]) for a in bus.on(PROPOSAL)[0]["actions"]] == expected


def test_slow_mode_times_out(run_case, bus, llm_cfg):
    slept = []
    _, outcome = run_case("B2", mock(llm_cfg, "slow", sleep=slept.append))
    assert outcome == "timeout" and bus.on(PROPOSAL) == []
    assert slept and slept[0] <= llm_cfg["budget"]["answer_s"]


def test_offline_mode(run_case, bus, llm_cfg):
    backend = mock(llm_cfg, "offline")
    assert backend.health() == (False, "mock LLM in offline mode")
    _, outcome = run_case("B2", backend)
    assert outcome == "timeout"
    assert bus.on("brain/internal/llm-status")[0]["status"] == "offline"


def test_invalid_mode(run_case, bus, llm_cfg):
    _, outcome = run_case("B2", mock(llm_cfg, "invalid"))
    assert outcome == "invalid" and bus.events("llm_invalid_output")


def test_unknown_mode_is_refused(llm_cfg):
    with pytest.raises(ValueError):
        mock(llm_cfg, "chaotic")


def test_control_topic_switches_the_mock_mode(run_case, llm_cfg):
    """brain/control/llm (scenario runner): mock only, plain JSON, bad messages ignored."""
    from mocks.mock_llm.control import LlmControl
    from src.common import config
    from src.common.log import EventLogger

    topic = config.load("topics")["control"]["llm"]["topic"]
    backend = mock(llm_cfg, "scripted")
    control = LlmControl(backend, topic, EventLogger("test-llm-control", None, "ERROR"))
    control.handle(topic, json.dumps({"mode": "offline"}))
    assert backend.mode == "offline" and backend.health()[0] is False and backend.name == "mock:offline"
    for bad in ("not json", json.dumps({"mode": "nope"}), json.dumps(["offline"]), json.dumps({})):
        control.handle(topic, bad)
    control.handle("brain/control/run", json.dumps({"mode": "scripted"}))   # another topic: ignored
    assert backend.mode == "offline"
    assert run_case("B2", backend)[1] == "timeout"
    control.handle(topic, json.dumps({"mode": "scripted"}))
    assert run_case("B2", backend)[1] == "proposal"


# --- Ollama backend against a local fake HTTP server (no model needed) ----------------


class FakeOllama(BaseHTTPRequestHandler):
    """Speaks enough of the Ollama API: /api/tags, /api/generate, streamed /api/chat."""

    reply: dict = {}                 # for /api/chat: {"content": "..."} or {"error": "..."}
    delay_s = 0.0                    # before the first byte
    token_delay_s = 0.0              # between streamed chunks
    seen: list = []

    def do_GET(self):
        self._send({"models": [{"name": "tiny:1b", "model": "tiny:1b", "digest": "abc123def456" + "0" * 52}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.seen.append((self.path, body))
        if FakeOllama.delay_s:
            threading.Event().wait(FakeOllama.delay_s)
        if not body.get("stream"):
            return self._send(FakeOllama.reply)
        if "error" in FakeOllama.reply:
            chunks = [{"error": FakeOllama.reply["error"]}]
        else:
            text = FakeOllama.reply.get("content", "")
            chunks = [{"message": {"content": text[i:i + 8]}, "done": False} for i in range(0, len(text), 8)]
            chunks.append({"message": {"content": ""}, "done": True})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for c in chunks:
                self.wfile.write((json.dumps(c) + "\n").encode())
                self.wfile.flush()
                if FakeOllama.token_delay_s:
                    threading.Event().wait(FakeOllama.token_delay_s)
        except OSError:
            pass                                    # client closed the stream (deadline test)

    def _send(self, doc):
        data = json.dumps(doc).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass                                    # client gave up (timeout test)

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOllama.reply, FakeOllama.delay_s, FakeOllama.token_delay_s, FakeOllama.seen = {}, 0.0, 0.0, []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def backend_for(url, llm_cfg, model="tiny:1b"):
    return OllamaBackend(llm_cfg["backend"] | {"url": url, "model": model, "digest": ""})


VALVE_ANSWER = json.dumps(
    {"actions": [{"target_id": "valve-port-up", "action": "open", "params": {}}], "justification": "ok"})


def test_ollama_request_uses_schema_and_temperature_zero(ollama, run_case, bus, llm_cfg):
    FakeOllama.reply = {"content": VALVE_ANSWER}
    _, outcome = run_case("B2", backend_for(ollama, llm_cfg))
    assert outcome == "proposal"
    path, body = FakeOllama.seen[0]
    assert path == "/api/chat" and body["stream"] is True
    assert body["options"]["temperature"] == 0
    assert "valve-port-up" in body["format"]["properties"]["actions"]["items"]["properties"]["target_id"]["enum"]
    assert body["messages"][0]["role"] == "system" and "INCIDENT inc-0031" in body["messages"][1]["content"]


def test_ollama_health(ollama, llm_cfg):
    assert backend_for(ollama, llm_cfg).health() == (True, None)
    ok, reason = backend_for(ollama, llm_cfg, "missing:7b").health()
    assert not ok and "not pulled" in reason
    ok, reason = backend_for("http://127.0.0.1:9", llm_cfg).health()
    assert not ok and "unreachable" in reason


def test_ollama_digest_pin(ollama, llm_cfg):
    pinned = OllamaBackend(llm_cfg["backend"] | {"url": ollama, "model": "tiny:1b", "digest": "abc123"})
    assert pinned.health() == (True, None)
    other = OllamaBackend(llm_cfg["backend"] | {"url": ollama, "model": "tiny:1b", "digest": "ffff00"})
    ok, reason = other.health()
    assert not ok and "config pins ffff00" in reason
    unpinned = OllamaBackend(llm_cfg["backend"] | {"url": ollama, "model": "tiny:1b", "digest": ""})
    assert unpinned.health() == (True, None)


def test_ollama_timeout_and_unreachable(ollama, llm_cfg):
    from src.llm.prompt import Prompt
    p = Prompt("v", "s", "u", {}, None)
    FakeOllama.delay_s = 1.0
    with pytest.raises(BackendTimeout):
        backend_for(ollama, llm_cfg).generate(p, timeout_s=0.2)
    with pytest.raises(BackendUnavailable):
        backend_for("http://127.0.0.1:9", llm_cfg).generate(p, timeout_s=5)   # Windows retries a refused connect for ~2 s


def test_ollama_deadline_is_hard_even_while_tokens_flow(ollama, llm_cfg):
    from src.llm.prompt import Prompt
    FakeOllama.reply, FakeOllama.token_delay_s = {"content": VALVE_ANSWER}, 0.1   # ~10 chunks -> ~1 s
    t0 = time.monotonic()
    with pytest.raises(BackendTimeout):
        backend_for(ollama, llm_cfg).generate(Prompt("v", "s", "u", {}, None), timeout_s=0.3)
    assert time.monotonic() - t0 < 0.8


def test_ollama_streamed_error(ollama, llm_cfg):
    from src.llm.prompt import Prompt
    FakeOllama.reply = {"error": "model crashed"}
    with pytest.raises(BackendUnavailable, match="model crashed"):
        backend_for(ollama, llm_cfg).generate(Prompt("v", "s", "u", {}, None), timeout_s=1)


def test_ollama_warmup_uses_generate(ollama, llm_cfg):
    backend_for(ollama, llm_cfg).warmup(timeout_s=1)
    assert FakeOllama.seen[0][0] == "/api/generate"


# --- optional: the real model (LLM_LIVE=1, Ollama running with the configured model) ----


@pytest.mark.skipif(os.environ.get("LLM_LIVE") != "1", reason="set LLM_LIVE=1 with Ollama running")
def test_live_model_meets_the_targets(llm_cfg):
    r = bench_model(llm_cfg["backend"]["model"], llm_cfg, load_cases(), repeat=1)
    assert "error" not in r, r.get("error")
    assert r["max_s"] <= llm_cfg["budget"]["answer_s"]
    assert r["injection_safe"]
