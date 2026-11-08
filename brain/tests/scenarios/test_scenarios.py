"""Scenario files and runner logic (scenarios/), without a broker.

- every scenario file is valid (Test Book fields, known check kinds, ids of the mock city)
- TESTBOOK.md is generated from them and up to date
- the evaluation (pure functions over observed messages) on synthetic message lists
- each scenario's control plan played through the Twin mock, the Guardian mock and the Brain's real
  input module: every check those three can satisfy alone (readings, verdicts, quarantines, input
  errors, layer status) passes. The checks on decisions and commands need the other Brain modules.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parents[3] / "scenarios"
sys.path.insert(0, str(SCENARIOS))

def _load_runner():
    """scenarios/run.py under its own name: brain/run.py (the launcher) is also called `run`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scenario_runner", SCENARIOS / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import make_testbook  # noqa: E402
import scenariolib as lib  # noqa: E402

from src.common.envelope import seal  # noqa: E402
from tests.mocks.conftest import GUARDIAN_KEY, T0, TWIN_KEY  # noqa: E402
from tests.mocks.test_guardian import GuardianRig  # noqa: E402

CITY = lib.city_index()
ALL = lib.list_scenarios()


# --- files ----------------------------------------------------------------------------


def test_all_twelve_scenarios_and_t5_exist():
    assert {f"B{i}" for i in range(1, 13)} <= set(ALL) and "T5" in ALL


@pytest.mark.parametrize("sid", ALL)
def test_scenario_file_is_valid(sid):
    sc = lib.load_scenario(sid)
    assert sc["id"] == sid
    assert lib.validate(sc, CITY) == []
    assert sc["duration_s"] <= 90


def test_t5_is_b3_under_its_own_id():
    t5, b3 = lib.load_scenario("T5"), lib.load_scenario("B3")
    assert t5["expect"] == b3["expect"] and t5["trigger"] == "python scenarios/run.py T5"


def test_testbook_is_up_to_date():
    assert make_testbook.is_current(), "run python scenarios/make_testbook.py"
    text = make_testbook.render()
    for sid in ALL:
        assert f"## {sid}" in text and f"python scenarios/run.py {sid}" in text


@pytest.mark.parametrize("broken, problem", [
    ({"layers": ["twin", "cloud"]}, "layers"),
    ({"trigger": "make B3"}, "trigger"),
    ({"duration_s": 120}, "duration_s"),
    ({"expected": {"twin": "x", "brain": "y"}}, "expected"),
    ({"expect": [{"command": {"target_type": "laser"}}]}, "target_type"),
    ({"expect": [{"command": {"target_id": "valve-99"}}]}, "target_id"),
    ({"expect": [{"teleport": "x"}]}, "check kind"),
    ({"expect": [{"verdict": "rejected", "within_s": 500}]}, "within_s"),
    ({"expect": [{"reading": "ghost-01"}]}, "unknown sensor"),
    ({"guardian": {"modes": ["quarantine:valve-99"]}}, "unknown device"),
    ({"guardian": {"modes": ["dance"]}}, "guardian"),
    ({"llm": {"mode": "genius"}}, "llm"),
    ({"twin": {"faults": [{"mode": "meteor", "tick": 3}]}}, "unknown mode"),
    ({"twin": {"reset": {"scenario_zone": "zone-99"}}}, "unknown zone"),
    ({"twin": {"actions": [{"at_s": 100, "op": "trigger", "scenario": "fire"}]}}, "at_s"),
    ({"twin": {"actions": [{"at_s": 1, "op": "call", "zone_id": "zone-01", "category": "fire", "text": "x",
                            "repeat": {"count": 99, "every_s": 5}}]}}, "repeat"),
    ({"name": ""}, "name"),
])
def test_validation_finds_problems(broken, problem):
    sc = {**lib.load_scenario("B3"), **broken}
    problems = lib.validate(sc, CITY)
    assert problems and any(problem in p for p in problems), problems


# --- plan -----------------------------------------------------------------------------


def test_plan_starts_with_full_resets_and_expands_repeats():
    steps = lib.plan(lib.load_scenario("B7"))
    assert [s.channel for s in steps[:3]] == ["llm", "guardian", "twin"]
    reset = steps[2].body
    assert reset["op"] == "reset" and set(lib.RESET_KEYS) <= set(reset)
    calls = [s for s in steps if s.body.get("op") == "call"]
    assert [s.at_s for s in calls] == [5 + 6 * i for i in range(10)]
    assert [s.at_s for s in steps] == sorted(s.at_s for s in steps)


def test_plan_guardian_and_llm_actions():
    b4 = lib.plan(lib.load_scenario("B4"))
    assert any(s.channel == "guardian" and s.at_s == 3 and s.body == {"op": "mode", "modes": ["offline"]} for s in b4)
    b12 = lib.plan(lib.load_scenario("B12"))
    assert b12[0].channel == "llm" and b12[0].body == {"mode": "offline"}


def test_reading_subscriptions_cover_reading_checks():
    assert lib.reading_subscriptions(lib.load_scenario("B3"), CITY) == ["city/zone-01/sensor/water_level/water-01"]


# --- evaluation on synthetic messages ---------------------------------------------------


def msg(t, on, retained=False, **body):
    return {"t": t, "topic": on, "retained": retained, "body": body}


FLOOD_RUN = [
    msg(0, "twin/actuator/valve-01", retained=True, actuator_id="valve-01", result="done"),
    msg(7, "brain/internal/incident", incident_id="inc-1", type="flood", zone_id="zone-01", status="opened", verified=True),
    msg(9, "brain/internal/decision", incident_id="inc-1", origin="rules", mode="normal",
        actions=[{"target_id": "valve-01", "action": "open", "params": {}}]),
    msg(10, "brain/cmd/valve-01", id="brain-000001", target_id="valve-01", action="open", parameters={}),
    msg(12, "brain/cmd/valve-01", id="brain-000001", target_id="valve-01", action="open", parameters={}),  # retry
    msg(13, "twin/actuator/valve-01", actuator_id="valve-01", result="done", correlation_id="brain-000001"),
    msg(14, "brain/internal/outcome", target_id="valve-01", action="open", status="acknowledged"),
    msg(15, "brain/event", event="device_quarantined", summary="water-02 quarantined by Guardian (forced)"),
    msg(16, "brain/error", code="E_AUTH", topic="city/zone-01/sensor/water_level/water-01", detail="x"),
    msg(20, "city/zone-01/sensor/water_level/water-01", sensor_id="water-01", value=0.8),
    msg(30, "brain/cmd/unit-fire-01", id="brain-000002", target_id="unit-fire-01", action="dispatch",
        parameters={"zone_id": "zone-05"}),
]


def check(entry, observed=FLOOD_RUN, duration=60):
    return lib.evaluate_entry(entry, observed, CITY, duration)


@pytest.mark.parametrize("entry, passed", [
    ({"event": "incident_opened", "match": {"zone": "zone-01", "type": "flood"}, "within_s": 20}, True),   # derived
    ({"event": "incident_opened", "match": {"zone": "zone-02"}}, False),
    ({"event": "incident_opened", "within_s": 5}, False),
    ({"event": "device_quarantined", "text": "WATER-02"}, True),
    ({"command": {"target_type": "valve", "action": "open"}, "within_s": 30}, True),
    ({"command": {"target_type": "valve", "action": "close"}}, False),
    ({"command": {"target_type": "fire_truck", "target_zone": "zone-05"}}, True),
    ({"command": {"target_type": "fire_truck", "target_zone": "zone-03"}}, False),
    ({"no_command": {"target_type": "power_grid"}}, True),
    ({"no_command": {"target_type": "valve"}}, False),
    ({"no_command": "any", "after_s": 31}, True),
    ({"max_commands": {"target_id": "valve-01", "count": 1}}, True),       # the retry keeps its id
    ({"max_commands": {"count": 1}}, False),
    ({"decision": {"origin": "rules"}}, True),
    ({"decision": "llm"}, False),
    ({"decision": {"target_type": "valve", "mode": "normal"}}, True),
    ({"no_decision": {"target_type": ["power_grid", "traffic_signal"]}}, True),
    ({"outcome": {"status": "acknowledged", "target_id": "valve-01"}}, True),
    ({"outcome": {"status": "refused", "rule": "S1"}}, False),
    ({"error_code": "E_AUTH"}, True),
    ({"error_code": ["E_RANGE", "E_STALE"]}, False),
    ({"no_error_code": "any"}, False),
    ({"ack": "done", "match": {"actuator_id": "valve-01"}, "within_s": 5}, False),   # only the retained one
    ({"ack": "done", "match": {"actuator_id": "valve-01"}}, True),
    ({"reading": "water-01", "match": {"value": {"lt": 1.0}}}, True),
    ({"reading": "water-01", "match": {"value": {"gt": 1.0, "le": 3}}}, False),
    ({"incident": "flood", "match": {"verified": False}}, False),
    ({"decision": "any", "count": 2}, False),
    ({"any_of": [{"outcome": "refused"}, {"decision": {"target_id": "valve-01"}}]}, True),
    ({"any_of": [{"outcome": "refused"}, {"alert": "actuator_no_effect"}]}, False),
])
def test_evaluate_entry(entry, passed):
    r = check(entry)
    assert r.passed is passed, r.detail


def test_failed_checks_say_why():
    r = check({"no_command": {"target_type": "valve"}})
    assert "10.0 s" in r.detail and "brain/cmd/valve-01" in r.detail
    r = check({"alert": "actuator_no_effect"})
    assert "0 of 1" in r.detail


def test_optional_check_never_fails_the_run():
    sc = {"id": "X", "name": "x", "duration_s": 60,
          "expect": [{"decision": "rules"}, {"proposal": "llm", "optional": True}]}
    results = lib.evaluate(sc, FLOOD_RUN, CITY)
    assert [r.passed for r in results] == [True, False] and lib.overall(results)
    assert "[WARN]" in "\n".join(lib.report_lines(sc, results))
    sc["expect"].append({"alert": "actuator_no_effect"})
    assert not lib.overall(lib.evaluate(sc, FLOOD_RUN, CITY))


def test_decode_envelope_and_plain_json():
    body = {"id": "x", "event": "decision"}
    assert lib.decode(seal(body, b"k")) == body
    assert lib.decode(json.dumps(body)) == body
    assert lib.decode("not json") is None and lib.decode("[1]") is None


def test_city_from_meta_matches_the_city_file():
    from mocks.twin.core import Twin
    meta = Twin().meta()
    other = lib.city_from_meta(meta)
    assert other.targets == CITY.targets and other.zones == CITY.zones and set(other.sensors) == set(CITY.sensors)


# --- the runner with a fake client and a fake clock ------------------------------------------


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += max(0.0, s)


class FakeClient:
    def __init__(self, ftime, inject):
        self.ftime, self.inject = ftime, inject
        self.on_message = None
        self.published: list[tuple] = []
        self.subs: list[str] = []

    def subscribe(self, topic, qos=1):
        self.subs.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        for t, body in self.inject(topic, payload):    # what the city "answers"
            self.on_message(t, json.dumps(body).encode(), False)


def test_run_one_publishes_run_control_drives_mocks_and_writes_one_log(tmp_path, monkeypatch):
    runner = _load_runner()
    from src.common import config

    ftime = FakeTime()
    monkeypatch.setattr(runner, "time", ftime)
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)

    def inject(topic, payload):
        body = lib.decode(payload)
        if topic == "twin/control" and body.get("op") == "reset":   # pretend the Brain refuses one reading
            return [("brain/error", {"id": "e1", "code": "E_FORMAT", "topic": "x", "detail": "d"})]
        return []

    client = FakeClient(ftime, inject)
    keys = {"twin": TWIN_KEY, "guardian": GUARDIAN_KEY}
    passed = runner.run_one("B9", client, keys, config.load("topics"))
    assert passed is False                                       # only E_FORMAT was seen
    topics = [t for t, *_ in client.published]
    assert topics[0] == "brain/control/run" and client.published[0][3] is True
    start, stop = [json.loads(p) for t, p, *_ in client.published if t == "brain/control/run"]
    assert (start["action"], stop["action"], stop["result"]) == ("start", "stop", "FAIL")
    assert start["scenario_id"] == "B9" and start["run_id"] == stop["run_id"]
    twin_msgs = [json.loads(p) for t, p, *_ in client.published if t == "twin/control"]
    assert all("hmac" in m for m in twin_msgs)                   # sealed for the Twin mock
    assert {"brain/event", "brain/cmd/+", "guardian/verdict/+"} <= set(client.subs)
    logs = list(tmp_path.glob("B9-*.log"))
    assert len(logs) == 1
    lines = [json.loads(x) for x in logs[0].read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "run" and lines[-1]["kind"] == "summary" and lines[-1]["result"] == "FAIL"
    assert any(x["kind"] == "observed" and x["topic"] == "brain/error" for x in lines)
    assert [e["passed"] for e in lines[-1]["expectations"]][0] is True       # the E_FORMAT check


def test_cli_check_and_list(capsys):
    runner = _load_runner()
    assert runner.main(["--check"]) == 0
    assert runner.main(["--list"]) == 0
    assert runner.main(["B99"]) == 2
    assert "B13" in capsys.readouterr().out


# --- the plan played through the mocks and the real input module ---------------------------

# Checks the Twin mock, the Guardian mock and the input module can satisfy alone
MOCK_KINDS = {"reading", "verdict", "quarantine", "error_code", "layer"}
INPUT_EVENTS = {"device_quarantined", "device_released", "layer_offline", "layer_online"}


def mock_side(entry: dict) -> bool:
    kind = next(k for k in entry if k in lib.CHECK_KINDS)
    if kind == "any_of":
        return False
    base = kind.removeprefix("no_")
    if base == "event":
        return entry[kind] in INPUT_EVENTS
    return base in MOCK_KINDS


class ScenarioSim:
    """One simulated second per Twin tick; control messages sent at their offsets."""

    def __init__(self, tmp_path):
        self.rig = GuardianRig(tmp_path)
        self.observed: list[dict] = []
        self.marks = {"twin": 0, "guardian": 0, "brain": 0}

    def collect(self, before_start: bool = False):
        t = round(self.rig.clock.t - T0, 3)
        for name, bus in (("twin", self.rig.twin_bus), ("guardian", self.rig.g_bus), ("brain", self.rig.brain_bus)):
            for topic, payload, _q, retain in bus.sent[self.marks[name]:]:
                if name == "brain" and topic.startswith("brain/internal/") and not topic.startswith("brain/internal/layer-status"):
                    continue            # what the runner observes: no trust / reading copies
                self.observed.append({"t": t, "topic": topic, "retained": before_start and retain,
                                      "body": lib.decode(payload)})
            self.marks[name] = len(bus.sent)

    def send(self, step):
        if step.channel == "twin":
            self.rig.twin.handle("twin/control", seal(step.body, TWIN_KEY))
            self.rig.feed()
        elif step.channel == "guardian":
            self.rig.control(step.body)

    def run(self, sc) -> list[dict]:
        self.rig.connect()
        self.collect(before_start=True)
        steps = lib.plan(sc)
        for second in range(int(sc["duration_s"]) + 1):
            for step in [s for s in steps if second <= s.at_s < second + 1]:
                self.send(step)
            self.collect()
            self.rig.tick()
            self.collect()
        return self.observed


@pytest.mark.parametrize("sid", ALL)
def test_mock_side_checks_pass_when_the_plan_is_played(tmp_path, sid):
    sc = lib.load_scenario(sid)
    observed = ScenarioSim(tmp_path).run(sc)
    entries = [e for e in sc["expect"] if mock_side(e)]
    results = [lib.evaluate_entry(e, observed, CITY, sc["duration_s"]) for e in entries]
    failed = [f"{r.check}: {r.detail}" for r in results if not r.passed]
    assert failed == [], "\n".join(failed)


def test_fake_flood_is_caught_within_10_s_of_the_first_fake_reading(tmp_path):
    observed = ScenarioSim(tmp_path).run(lib.load_scenario("B3"))
    first_fake = min(m["t"] for m in observed if m["topic"].endswith("/water-01") and "/sensor/" in m["topic"]
                     and m["body"]["value"] == 1.8)
    rejected = min(m["t"] for m in observed if m["topic"] == "guardian/verdict/water-01"
                   and m["body"]["verdict"] == "rejected")
    assert rejected - first_fake < 10
    assert not [m for m in observed if m["topic"] == "brain/error"]


def test_bad_messages_give_exactly_the_six_codes(tmp_path):
    observed = ScenarioSim(tmp_path).run(lib.load_scenario("B9"))
    codes = sorted(m["body"]["code"] for m in observed if m["topic"] == "brain/error")
    assert codes == sorted(["E_FORMAT", "E_AUTH", "E_REPLAY", "E_UNKNOWN_ID", "E_RANGE", "E_STALE"])
