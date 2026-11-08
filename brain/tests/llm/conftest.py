from __future__ import annotations

import copy
import json
from typing import Any, Callable

import pytest
from pathlib import Path

from src.common import config
from src.common.log import EventLogger
from src.llm.backends import BackendTimeout
from src.llm.bench import feed, load_cases
from src.llm.prompt import Prompt
from src.llm.service import LlmService

DATA = load_cases(Path(__file__).parent / "cases.yaml")   # small unit-test city
CASES = {c["id"]: c for c in DATA["cases"]}
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Bus:
    def __init__(self):
        self.sent: list[tuple[str, str, int, bool]] = []

    def __call__(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))

    def on(self, topic: str) -> list[dict]:
        out = []
        for t, payload, _, _ in self.sent:
            if t == topic:
                doc = json.loads(payload)
                out.append(json.loads(doc["body"]) if "body" in doc else doc)
        return out

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.on("brain/event") if name is None or e["event"] == name]


class FakeBackend:
    """Returns a fixed answer, or what `answer(prompt)` returns; can burn clock time."""

    name = "fake:test"

    def __init__(self, answer: str | Callable[[Prompt], str], clock: Clock | None = None,
                 takes_s: float = 0.5, healthy: bool = True):
        self.answer = answer
        self.clock = clock
        self.takes_s = takes_s
        self.healthy = healthy
        self.prompts: list[Prompt] = []

    def generate(self, prompt: Prompt, timeout_s: float) -> str:
        self.prompts.append(prompt)
        if self.clock is not None:
            if self.takes_s > timeout_s:
                self.clock.t += timeout_s
                raise BackendTimeout("fake timeout")
            self.clock.t += self.takes_s
        return self.answer(prompt) if callable(self.answer) else self.answer

    def health(self):
        return (True, None) if self.healthy else (False, "fake down")

    def warmup(self, timeout_s: float) -> None:
        pass


def answer(*actions: tuple[str, str], justification: str = "Because.") -> str:
    return json.dumps({"actions": [{"target_id": t, "action": a, "params": {}} for t, a in actions],
                       "justification": justification})


# The unit tests use the small city of cases.yaml, whose device types and actions differ from
# the demo city (config/llm.yaml follows brain/mocks/city.yaml). Its policy is set here.
TEST_CITY_POLICY = {
    "relevant_targets": {
        "flood": ["valve", "traffic_signal", "rescue", "ambulance"],
        "fire": ["fire", "ambulance", "traffic_signal", "grid_switch"],
        "accident": ["ambulance", "police", "rescue", "traffic_signal"],
        "traffic_jam": ["traffic_signal", "police"],
        "power_problem": ["grid_switch", "traffic_signal"],
    },
    "action_hints": {
        "valve": {"open": "drains water from the zone", "close": "stops draining"},
        "traffic_signal": {"retime": "eases congestion", "close_inbound": "stops cars entering",
                           "green_corridor": "clears a route for emergency units", "divert": "sends traffic around"},
        "grid_switch": {"switch_backup": "feeds the zone from the backup line", "isolate": "cuts power to the zone"},
    },
    "playbook": {
        "flood": [["valve", "open"], ["traffic_signal", "close_inbound"]],
        "fire": [["fire", "dispatch"], ["traffic_signal", "green_corridor"]],
        "accident": [["ambulance", "dispatch"], ["traffic_signal", "divert"]],
        "traffic_jam": [["traffic_signal", "retime"]],
        "power_problem": [["grid_switch", "switch_backup"]],
    },
}


@pytest.fixture
def llm_cfg() -> dict[str, Any]:
    cfg = copy.deepcopy(config.load("llm"))
    cfg["logging"] = {"file": None, "level": "ERROR", "log_prompts": False}
    cfg["relevant_targets"] = copy.deepcopy(TEST_CITY_POLICY["relevant_targets"])
    cfg["action_hints"] = copy.deepcopy(TEST_CITY_POLICY["action_hints"])
    cfg["mock"]["playbook"] = copy.deepcopy(TEST_CITY_POLICY["playbook"])
    cfg["context"]["neighbour_actuators"] = True      # the test city's cases rely on neighbouring devices
    return cfg


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def make_service(llm_cfg, clock, bus):
    def make(backend, **kw) -> LlmService:
        return LlmService(bus, backend, clock=clock, llm_cfg=kw.pop("cfg", llm_cfg),
                          logger=EventLogger("test-llm", None, "ERROR"), **kw)
    return make


@pytest.fixture
def run_case(make_service):
    """Feed a case from cases.yaml and process its incident; returns (service, outcome)."""
    def run(case_id: str, backend, case_overrides: dict | None = None, **kw):
        case = copy.deepcopy(CASES[case_id])
        case.update(case_overrides or {})
        service = make_service(backend, **kw)
        feed(service, DATA["city"], case)
        return service, service.process(service.next_job(0))
    return run
