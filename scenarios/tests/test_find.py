"""Context.find(): plain values must be equal, and a function value is a test that must return True to match."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from run_scenario import Context  # noqa: E402


def make_ctx(events):
    ctx = Context.__new__(Context)          # no MQTT, no log file: find() only needs events and a lock
    ctx.events, ctx.lock = events, threading.Lock()
    return ctx


EVENTS = [
    {"t": 10, "topic": "guardian/verdicts/WATER_08", "msg": {"sensor_id": "WATER_08", "status": "untrusted", "trust": 0.1}},
    {"t": 11, "topic": "guardian/verdicts/WATER_07", "msg": {"sensor_id": "WATER_07", "status": "trusted", "trust": 0.95}},
    {"t": 12, "topic": "brain/decisions", "msg": {"event": "command_sent", "actuator": "VALVE_03"}},
]


def test_plain_value_matches_equal_fields():
    found = make_ctx(EVENTS).find(0, "guardian/verdicts/", status="untrusted")
    assert [e["msg"]["sensor_id"] for e in found] == ["WATER_08"]


def test_function_matcher_accepts_what_it_returns_true_for():
    found = make_ctx(EVENTS).find(0, "guardian/", trust=lambda v: v is not None and v < 0.5)
    assert [e["msg"]["sensor_id"] for e in found] == ["WATER_08"]


def test_function_matcher_rejects_what_it_returns_false_for():
    assert make_ctx(EVENTS).find(0, trust=lambda v: v is not None and v > 2) == []


def test_since_and_topic_prefix_filter():
    ctx = make_ctx(EVENTS)
    assert len(ctx.find(11)) == 2
    assert [e["topic"] for e in ctx.find(0, "brain/")] == ["brain/decisions"]
