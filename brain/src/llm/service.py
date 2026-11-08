"""LLM service: incident in, validated proposal out (team doc 03-llm).

Independent of MQTT, like the input service: `handle()` takes (topic, payload, retained)
and the service calls `publish`. Incidents are queued and answered one at a time by
`process()`, which a worker thread calls in production and tests call directly.

Time budget: the clock starts when the incident is received. Waiting in the queue,
the graph call and the LLM all count. Past the budget the answer is dropped and
`llm_timeout` published; the rule fallback's proposal is then used by the math team.
A newer update of the same incident replaces the older request (only the latest
version is answered), and a closed incident cancels it. The GPU answers one incident
at a time, so the queue serves the most severe incidents first (then the oldest).
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from ..common import config
from ..common.events import publish_event
from ..common.ids import IdGenerator
from ..common.log import EventLogger, utc_now_iso
from ..common.models import SEVERITY_RANK, LlmStatus, Proposal
from . import prompt as prompts
from . import validate
from .backends import BackendTimeout, BackendUnavailable, LlmBackend
from .context import CityState, GraphContextClient, merge_graph
from .incident import Incident

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain



@dataclass(slots=True)
class Job:
    incident: Incident
    version: int
    received: float
    deadline: float


class LlmService:
    def __init__(
        self,
        publish: Publisher,
        backend: LlmBackend,
        *,
        graph: GraphContextClient | None = None,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        llm_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        seal_keys: dict[str, bytes] | None = None,
    ):
        self.cfg = llm_cfg or config.load("llm")
        self.topics = topics_cfg or config.load("topics")
        self._publish = publish
        self.backend = backend
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-llm", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.graph = graph
        self.seal_keys = seal_keys
        self.ids = IdGenerator("llm")
        ctx_cfg = self.cfg["context"]
        self.city = CityState(self.cfg.get("unit_actions", {}), float(ctx_cfg["recent_calls_window_s"]),
                              self.cfg.get("relevant_targets"), self.cfg.get("action_hints"),
                              self.cfg.get("hidden_actions"),
                              bool(self.cfg["context"].get("neighbour_actuators", True)))
        self.budget_s = float(self.cfg["budget"]["answer_s"])
        self._jobs: queue.PriorityQueue[tuple[int, int, Job]] = queue.PriorityQueue()
        self._latest: dict[str, int] = {}        # incident_id -> latest version (0 = closed)
        self._versions = 0
        self._lock = threading.Lock()
        self.status: str | None = None
        self._slow_in_row = 0                     # consecutive answers over the budget
        self._too_slow = False                    # offline because too slow, not unreachable
        self._last_health = float("-inf")

    # --- inputs -------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        return [
            (t["meta"]["topic"], 1),
            (t["incident"]["topic"], 1),
            (t["call"]["topic"], 1),
            (t["ack"]["topic"], 1),
            (t["quarantine"]["topic"].replace("{device_id}", "+"), 1),
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
        try:
            with self._lock:
                if topic == t["meta"]["topic"]:
                    self.city.apply_meta(msg)
                elif topic.startswith(t["quarantine"]["topic"].split("{")[0]):
                    self.city.apply_quarantine(msg)
                elif topic == t["ack"]["topic"]:
                    self.city.apply_ack(msg)
                elif topic == t["call"]["topic"]:
                    self.city.add_call(msg, self.clock())
            if topic == t["incident"]["topic"]:
                inc = Incident.model_validate(msg)
                with self._lock:
                    self.city.track_incident(inc)
                self.on_incident(inc)
        except (KeyError, TypeError, ValidationError) as e:
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])

    def on_incident(self, inc: Incident) -> None:
        now = self.clock()
        with self._lock:
            if inc.status == "closed":
                self._latest[inc.incident_id] = 0          # cancels a pending request
                return
            self._versions += 1
            version = self._versions
            self._latest[inc.incident_id] = version
        rank = -SEVERITY_RANK.get(inc.severity or "", -1)      # most severe first; unknown last
        self._jobs.put((rank, version, Job(inc, version, now, now + self.budget_s)))

    def next_job(self, timeout_s: float | None = None) -> Job:
        """Most severe queued incident, oldest first. Raises queue.Empty after timeout_s
        (0 = do not wait)."""
        if timeout_s == 0:
            return self._jobs.get_nowait()[2]
        return self._jobs.get(timeout=timeout_s)[2]

    def has_jobs(self) -> bool:
        return not self._jobs.empty()

    # --- one request -----------------------------------------------------------------

    def process(self, job: Job) -> str:
        """Answer one queued incident. Returns what happened (for logs and tests)."""
        inc = job.incident
        if self._superseded(job):
            self.log.info("llm_request_dropped", incident_id=inc.incident_id, reason="newer update or closed")
            return "superseded"
        if self.clock() >= job.deadline:
            return self._timeout(job, "budget used up while waiting in the queue")

        with self._lock:
            has_meta = self.city.has_meta
            ctx = self.city.build(inc, self.clock(), int(self.cfg["context"]["max_calls"])) if has_meta else None
        if ctx is None:
            return self._skip(job, "no city description yet")
        if self.graph is not None:
            wait = min(float(self.cfg["budget"]["context_s"]), job.deadline - self.clock())
            doc = self.graph.fetch(inc.incident_id, wait) if wait > 0 else None
            if doc is not None:
                ctx = merge_graph(ctx, doc, bool(self.cfg["context"].get("use_history", False)))
        if not ctx.targets:
            return self._skip(job, "no device or unit available for this zone")

        if self.status == "offline":
            # known down: hand over to the rule fallback at once (health tick brings it back)
            return self._timeout(job, "LLM offline")
        p = prompts.build(ctx, self.cfg)
        remaining = job.deadline - self.clock()
        if remaining <= 0:
            return self._timeout(job, "budget used up before the LLM call")
        started = self.clock()
        try:
            answer = self.backend.generate(p, remaining)
        except BackendTimeout:
            # slowness of the model only counts if it had a fair share of the budget: a job that
            # waited in the queue behind other incidents and got 1 s says nothing about the model
            fair = float(self.cfg["budget"].get("fair_share", 0.6)) * self.budget_s
            if remaining >= fair:
                self._slow_in_row += 1
            limit = int(self.cfg["budget"].get("offline_after_timeouts", 2))
            if limit and self._slow_in_row >= limit:
                # reachable but too slow (cold model, GPU throttled on battery): say so, so the
                # scoring module decides on its rules at once instead of waiting the budget each time
                self._too_slow = True
                self.set_status("offline", f"{self._slow_in_row} answers in a row over the {self.budget_s:g} s budget")
            return self._timeout(job, f"no answer within {self.budget_s:g} s")
        except BackendUnavailable as e:
            self.set_status("offline", str(e)[:200])
            return self._timeout(job, f"LLM unavailable ({str(e)[:120]})")
        latency_s = self.clock() - started
        if self.cfg.get("logging", {}).get("log_prompts", True):
            self.log.info("llm_exchange", incident_id=inc.incident_id, correlation_id=inc.correlation_id,
                          prompt_version=p.version, model=self.backend.name, system=p.system, prompt=p.user,
                          answer=answer, latency_s=round(latency_s, 3), context_source=ctx.source)
        if self.clock() > job.deadline:
            return self._timeout(job, "answer arrived after the budget")
        if self._superseded(job):
            self.log.info("llm_answer_dropped", incident_id=inc.incident_id, reason="newer update or closed")
            return "superseded"
        self._slow_in_row = 0
        if self.status != "online":
            self._too_slow = False
            self.set_status("online")

        checked = validate.check(answer, ctx, self.cfg)
        if not checked.ok:
            self.log.warning("llm_invalid_output", incident_id=inc.incident_id, errors=checked.errors)
            self._event("llm_invalid_output", f"LLM answer refused for {inc.type} at {inc.zone_id}: "
                        + "; ".join(checked.errors)[:200], inc)
            return "invalid"

        proposal = Proposal(
            id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=inc.correlation_id,
            incident_id=inc.incident_id, origin="llm", actions=checked.actions,
            justification=checked.justification,
        )
        spec = self.topics["internal"]["proposal"]
        self._publish(spec["topic"], proposal.to_json(), int(spec["qos"]), bool(spec["retain"]))
        actions = ", ".join(f"{a.action} {a.target_id}" for a in checked.actions)
        self.log.info("llm_proposal", incident_id=inc.incident_id, proposal_id=proposal.id,
                      dropped=checked.errors, latency_s=round(latency_s, 3))
        self._event("llm_proposal", f"LLM proposal for {inc.type} at {inc.zone_id}: {actions}", inc)
        return "proposal"

    # --- status ---------------------------------------------------------------------

    def tick(self) -> None:
        """Re-check the runtime every health_every_s; publishes llm-status when it changes."""
        now = self.clock()
        if now - self._last_health < float(self.cfg["backend"].get("health_every_s", 10)):
            return
        self._last_health = now
        ok, reason = self.backend.health()
        if ok and self._too_slow:
            ok, reason = self._probe()               # reachable is not enough: it must answer in time
        was = self.status
        self.set_status("online" if ok else "offline", reason)
        if ok and was == "offline":
            # the runtime came back (restart, re-plug): its model is not loaded any more
            try:
                self.backend.warmup(timeout_s=120)
                self.log.info("llm_warmed_up", model=self.backend.name, after="recovery")
            except Exception as e:
                self.log.warning("llm_warmup_failed", error=repr(e)[:200])

    def _probe(self) -> tuple[bool, str | None]:
        """After repeated timeouts: a tiny real answer must come back within the budget."""
        probe = getattr(self.backend, "probe", None)
        if probe is None:
            return True, None
        try:
            elapsed = probe(self.budget_s)
        except Exception as e:  # timeout or unavailable: still too slow
            return False, f"still too slow ({type(e).__name__})"
        if elapsed > self.budget_s:
            return False, f"probe answered in {elapsed:.1f} s, over the {self.budget_s:g} s budget"
        self._too_slow, self._slow_in_row = False, 0
        self.log.info("llm_probe_ok", elapsed_s=round(elapsed, 2))
        return True, None

    def set_status(self, status: str, reason: str | None = None) -> None:
        if status == self.status:
            return
        self.status = status
        spec = self.topics["internal"]["llm_status"]
        self._publish(spec["topic"], self.status_payload(status, reason), int(spec["qos"]), True)
        self.log.info(f"llm_{status}", reason=reason, model=self.backend.name)
        self._event(f"llm_{status}", f"local LLM {status}" + (f" ({reason})" if reason else ""))

    def status_payload(self, status: str, reason: str | None = None) -> str:
        return LlmStatus(id=self.ids.next(), timestamp=utc_now_iso(), status=status,
                         model=self.backend.name, reason=reason).to_json()

    # --- helpers --------------------------------------------------------------------

    def _superseded(self, job: Job) -> bool:
        with self._lock:
            return self._latest.get(job.incident.incident_id) != job.version

    def _timeout(self, job: Job, why: str) -> str:
        inc = job.incident
        self.log.warning("llm_timeout", incident_id=inc.incident_id, reason=why)
        self._event("llm_timeout", f"LLM gave no proposal for {inc.type} at {inc.zone_id}: {why}", inc)
        return "timeout"

    def _skip(self, job: Job, why: str) -> str:
        inc = job.incident
        self.log.warning("llm_skipped", incident_id=inc.incident_id, reason=why)
        self._event("llm_skipped", f"LLM skipped {inc.type} at {inc.zone_id}: {why}", inc)
        return "skipped"

    def _event(self, event: str, summary: str, inc: Incident | None = None) -> None:
        publish_event(self._publish, self.topics, self.ids, self.seal_keys, event, summary,
                      correlation_id=inc.correlation_id if inc else None,
                      incident_id=inc.incident_id if inc else None)
