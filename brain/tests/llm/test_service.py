from __future__ import annotations

import copy
import json

from src.common.models import Proposal

from .conftest import CASES, DATA, FakeBackend, answer
from src.llm.bench import feed

PROPOSAL = "brain/internal/proposal"


def test_valid_answer_is_published_as_proposal(run_case, bus):
    _, outcome = run_case("B2", FakeBackend(answer(("valve-port-up", "open"), ("signal-port-2", "close_inbound"))))
    assert outcome == "proposal"
    [p] = bus.on(PROPOSAL)
    Proposal.model_validate(p)                                  # matches the shared contract
    assert p["origin"] == "llm"
    assert p["incident_id"] == "inc-0031"
    assert p["correlation_id"] == "twin-000182"                 # copied unchanged (overview section 3)
    assert p["id"].startswith("llm-")
    assert [a["target_id"] for a in p["actions"]] == ["valve-port-up", "signal-port-2"]
    [ev] = bus.events("llm_proposal")
    assert ev["correlation_id"] == "twin-000182" and "open valve-port-up" in ev["summary"]


def test_proposal_qos_and_not_retained(run_case, bus):
    run_case("B2", FakeBackend(answer(("valve-port-up", "open"))))
    [(_, _, qos, retain)] = [s for s in bus.sent if s[0] == PROPOSAL]
    assert qos == 1 and retain is False


def test_unknown_device_is_refused(run_case, bus):
    _, outcome = run_case("B2", FakeBackend(answer(("valve-everywhere", "open"))))
    assert outcome == "invalid"
    assert bus.on(PROPOSAL) == []
    [ev] = bus.events("llm_invalid_output")
    assert "unknown_target valve-everywhere" in ev["summary"]


def test_broken_json_is_refused(run_case, bus):
    _, outcome = run_case("B2", FakeBackend('{"actions": [{"target_id": "valve-'))
    assert outcome == "invalid" and bus.on(PROPOSAL) == []
    assert "not_json" in bus.events("llm_invalid_output")[0]["summary"]


def test_quarantined_valve_is_never_proposed(run_case, bus):
    backend = FakeBackend(answer(("valve-port-up", "open")))
    _, outcome = run_case("B8", backend)
    assert outcome == "invalid" and bus.on(PROPOSAL) == []
    # the quarantined valve is not offered to the model either
    p = backend.prompts[0]
    assert "valve-port-up" not in p.schema["properties"]["actions"]["items"]["properties"]["target_id"]["enum"]
    assert "QUARANTINED: valve-port-up" in p.user


def test_timeout_publishes_llm_timeout_and_no_proposal(run_case, bus, clock):
    _, outcome = run_case("B2", FakeBackend(answer(("valve-port-up", "open")), clock, takes_s=30))
    assert outcome == "timeout"
    assert bus.on(PROPOSAL) == []
    assert bus.events("llm_timeout")[0]["incident_id"] == "inc-0031"


def test_budget_counts_queue_time(make_service, bus, clock):
    service = make_service(FakeBackend(answer(("valve-port-up", "open"))))
    feed(service, DATA["city"], CASES["B2"])
    job = service.next_job(0)
    clock.t += 9                                                # waited longer than the 8 s budget
    assert service.process(job) == "timeout"
    assert bus.on(PROPOSAL) == []


def test_newer_update_replaces_older_request(make_service, bus):
    backend = FakeBackend(answer(("valve-port-up", "open")))
    service = make_service(backend)
    feed(service, DATA["city"], CASES["B2"])
    update = copy.deepcopy(CASES["B2"]["incident"]) | {"status": "updated", "severity": "critical"}
    service.handle("brain/internal/incident", json.dumps(update))
    first, second = service.next_job(0), service.next_job(0)
    assert first.incident.severity == "critical"                 # the newer, more severe version first
    assert service.process(first) == "proposal"
    assert service.process(second) == "superseded"
    assert len(backend.prompts) == 1 and "severity critical" in backend.prompts[0].user


def test_closed_incident_cancels_pending_request(make_service, bus):
    backend = FakeBackend(answer(("valve-port-up", "open")))
    service = make_service(backend)
    feed(service, DATA["city"], CASES["B2"])
    closed = copy.deepcopy(CASES["B2"]["incident"]) | {"status": "closed"}
    service.handle("brain/internal/incident", json.dumps(closed))
    assert service.process(service.next_job(0)) == "superseded"
    assert not service.has_jobs() and backend.prompts == []


def test_answer_for_an_incident_closed_meanwhile_is_dropped(make_service, bus):
    service = make_service(None)
    closed = copy.deepcopy(CASES["B2"]["incident"]) | {"status": "closed"}

    def close_while_thinking(prompt):
        service.handle("brain/internal/incident", json.dumps(closed))
        return answer(("valve-port-up", "open"))

    service.backend = FakeBackend(close_while_thinking)
    feed(service, DATA["city"], CASES["B2"])
    assert service.process(service.next_job(0)) == "superseded"
    assert bus.on(PROPOSAL) == []


def test_unavailable_runtime_sets_status_offline(run_case, bus):
    from src.llm.backends import BackendUnavailable

    class Down(FakeBackend):
        def generate(self, prompt, timeout_s):
            raise BackendUnavailable("connection refused")

    _, outcome = run_case("B2", Down(""))
    assert outcome == "timeout"
    [status] = bus.on("brain/internal/llm-status")
    assert status["status"] == "offline" and "connection refused" in status["reason"]
    assert [s[3] for s in bus.sent if s[0] == "brain/internal/llm-status"] == [True]    # retained


def test_health_tick_publishes_status_only_on_change(make_service, bus, clock):
    backend = FakeBackend("")
    service = make_service(backend)
    service.tick()
    clock.t += 1
    service.tick()                                              # too early, not re-checked
    backend.healthy = False
    clock.t += 10
    service.tick()
    clock.t += 10
    service.tick()                                              # still offline: nothing new
    assert [s["status"] for s in bus.on("brain/internal/llm-status")] == ["online", "offline"]


def test_no_city_description_yet_is_skipped(make_service, bus):
    backend = FakeBackend(answer(("valve-port-up", "open")))
    service = make_service(backend)
    service.handle("brain/internal/incident", json.dumps(CASES["B2"]["incident"]))
    assert service.process(service.next_job(0)) == "skipped"
    assert backend.prompts == [] and bus.events("llm_skipped")


def test_bad_internal_messages_never_raise(make_service):
    service = make_service(FakeBackend(""))
    service.handle("brain/internal/incident", b"not json")
    service.handle("brain/internal/incident", json.dumps({"incident_id": "x"}))
    service.handle("brain/internal/meta", json.dumps({"zones": [{"no_id": 1}]}))
    service.handle("brain/internal/call", json.dumps([1, 2]))
    assert not service.has_jobs()


def test_events_are_sealed_when_keys_are_given(run_case, bus):
    run_case("B2", FakeBackend(answer(("valve-port-up", "open"))), seal_keys={"twin": b"k1", "guardian": b"k2"})
    raw = json.loads([p for t, p, _, _ in bus.sent if t == "brain/event"][0])
    assert set(raw["hmacs"]) == {"twin", "guardian"}
    # internal topics stay plain JSON (overview section 2)
    assert "body" not in json.loads([p for t, p, _, _ in bus.sent if t == PROPOSAL][0])


def test_most_severe_incident_is_answered_first(make_service):
    service = make_service(FakeBackend(answer(("valve-port-up", "open"))))
    feed(service, DATA["city"], CASES["B2"])                    # high
    low = copy.deepcopy(CASES["B5"]["incident"]) | {"severity": "low", "incident_id": "inc-low"}
    critical = copy.deepcopy(CASES["B11"]["incident"]) | {"severity": "critical"}
    unknown = copy.deepcopy(CASES["B10"]["incident"]) | {"severity": None}
    for inc in (low, unknown, critical):
        service.handle("brain/internal/incident", json.dumps(inc))
    order = [service.next_job(0).incident.incident_id for _ in range(4)]
    assert order == ["inc-0070", "inc-0031", "inc-low", "inc-0060"]


def test_known_offline_runtime_is_not_called(run_case, bus, make_service, clock):
    backend = FakeBackend(answer(("valve-port-up", "open")), healthy=False)
    service = make_service(backend)
    service.tick()                                              # -> offline
    feed(service, DATA["city"], CASES["B2"])
    assert service.process(service.next_job(0)) == "timeout"
    assert backend.prompts == []
    assert "LLM offline" in bus.events("llm_timeout")[0]["summary"]


def test_model_is_warmed_up_again_after_recovery(make_service, clock):
    class Counting(FakeBackend):
        warmups = 0

        def warmup(self, timeout_s):
            Counting.warmups += 1

    backend = Counting("", healthy=True)
    service = make_service(backend)
    service.tick()                                              # first online: no extra warm-up
    backend.healthy = False
    clock.t += 10
    service.tick()                                              # offline
    backend.healthy = True
    clock.t += 10
    service.tick()                                              # back online -> warm-up
    assert Counting.warmups == 1


def test_too_slow_runtime_goes_offline_then_recovers_through_a_probe(make_service, bus, clock):
    """Reachable but too slow (cold model, GPU on battery): after two timeouts in a row llm-status goes
    offline, so scoring stops waiting the 8 s budget; a probe answering in time brings it back."""
    class Slow(FakeBackend):
        probe_s = 30.0

        def probe(self, timeout_s):
            return self.probe_s

    backend = Slow(answer(("valve-port-up", "open")), clock, takes_s=30)
    service = make_service(backend)
    service.tick()                                              # online
    for i in range(2):
        inc = copy.deepcopy(CASES["B2"]["incident"]) | {"incident_id": f"inc-slow-{i}"}
        feed(service, DATA["city"], CASES["B2"] | {"incident": inc})
        assert service.process(service.next_job(0)) == "timeout"
    statuses = bus.on("brain/internal/llm-status")
    assert statuses[-1]["status"] == "offline" and "2 answers in a row over the 8 s budget" in statuses[-1]["reason"]
    # while offline, incidents are handed over at once (no backend call)
    calls = len(backend.prompts)
    inc = copy.deepcopy(CASES["B2"]["incident"]) | {"incident_id": "inc-slow-x"}
    feed(service, DATA["city"], CASES["B2"] | {"incident": inc})
    assert service.process(service.next_job(0)) == "timeout" and len(backend.prompts) == calls
    clock.t += 10
    service.tick()                                              # probe still too slow: stays offline
    assert bus.on("brain/internal/llm-status")[-1]["status"] == "offline"
    backend.probe_s = 1.2
    clock.t += 10
    service.tick()
    assert bus.on("brain/internal/llm-status")[-1]["status"] == "online"


def test_one_timeout_alone_does_not_switch_off_the_llm(run_case, bus, clock):
    _, outcome = run_case("B2", FakeBackend(answer(("valve-port-up", "open")), clock, takes_s=30))
    assert outcome == "timeout"
    assert all(s["status"] != "offline" for s in bus.on("brain/internal/llm-status"))


def test_timeouts_after_queue_wait_do_not_switch_the_llm_off(make_service, bus, clock):
    """Several incidents at once: the last ones get what is left of the budget. Their timeouts do not
    mean the model is slow, so the circuit breaker must not open."""
    backend = FakeBackend(answer(("valve-port-up", "open")), clock, takes_s=30)
    service = make_service(backend)
    service.tick()
    for i in range(3):
        inc = copy.deepcopy(CASES["B2"]["incident"]) | {"incident_id": f"inc-q-{i}"}
        feed(service, DATA["city"], CASES["B2"] | {"incident": inc})
        job = service.next_job(0)
        clock.t += 6                                            # waited 6 of the 8 s in the queue
        assert service.process(job) == "timeout"
    assert all(s["status"] != "offline" for s in bus.on("brain/internal/llm-status"))
