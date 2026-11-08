"""Scoring service: rule fallback, waiting for the LLM, scoring, decision (team plan 04).

Independent of MQTT like the input and LLM services: `handle(topic, payload, retained)` takes
messages, `tick()` checks deadlines, and the service calls the injected `publish`.

Per incident (a "track"):

1. opened / updated -> a rule proposal (origin rules) is published at once.
2. The service waits for the LLM proposal up to the budget (llm.yaml budget.answer_s). It
   decides at once on the rules if llm-status is offline, if the LLM gave up on this incident
   (brain/event llm_timeout / llm_invalid_output / llm_skipped), or if severity is critical
   (then it switches if a better LLM proposal arrives before the budget ends).
3. Every proposal is checked (unknown target, action not allowed, quarantined, failed before:
   dropped), its units are allocated (free units, priority between incidents, pre-emption) and
   it is scored (model.py). The best positive score wins: brain/internal/decision + brain/event.
4. A later decision for the same incident (decision_changed) lists only actions not issued
   before: after a failed / refused action, when a unit becomes free, on severity increase or
   new evidence type. Nothing is undone (out of scope).
5. A closed incident is forgotten (its units are not recalled: out of scope).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from ..common import config
from ..common.envelope import EnvelopeError, parse_envelope, sign
from ..common.events import publish_event
from ..common.ids import IdGenerator
from ..common.log import EventLogger, utc_now_iso
from ..common.models import (
    SEVERITY_RANK,
    Decision,
    DecisionAlternative,
    Incident,
    Outcome,
    Proposal,
    ProposalAction,
)
from ..rules.engine import RuleEngine
from .city import CityView
from .model import ProposalScore, ScoringModel

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain


EVIDENCE_RULES = frozenset({"S3", "S6"})   # safety rules that refuse for lack of corroboration


@dataclass(slots=True)
class Track:
    incident: Incident
    first_seen: float
    deadline: float
    state: str = "waiting"                      # waiting | decided | skipped
    rules: Proposal | None = None
    llm: Proposal | None = None
    early: bool = False                         # decided before the budget (critical): may switch
    decisions: list[str] = field(default_factory=list)
    score: float = 0.0                          # score of the current decision
    issued: set[tuple[str, str]] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    wants_types: set[str] = field(default_factory=set)   # unit types it could not get
    rules_missing: set[str] = field(default_factory=set) # unit types the rule proposal could not get
    rules_notes: list[str] = field(default_factory=list) # what the rule proposal could not resolve
    redecisions: int = 0
    severity_rank: int = 0                      # at the last decision
    evidence_types: frozenset[str] = frozenset()
    skip_reason: str | None = None
    awaiting_evidence: int | None = None        # evidence count when S3/S6 refused: retry once there is more

    @property
    def id(self) -> str:
        return self.incident.incident_id


@dataclass(slots=True)
class Assignment:
    incident_id: str
    since: float = 0.0                          # when the dispatch was decided
    seen_busy: bool = False


class ScoringService:
    def __init__(
        self,
        publish: Publisher,
        *,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        scoring_cfg: dict[str, Any] | None = None,
        rules_cfg: dict[str, Any] | None = None,
        llm_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        seal_keys: dict[str, bytes] | None = None,
    ):
        self.cfg = scoring_cfg or config.load("scoring")
        self.rules_cfg = rules_cfg or config.load("rules")
        self.topics = topics_cfg or config.load("topics")
        self._publish = publish
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-scoring", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.seal_keys = seal_keys
        self.model = ScoringModel(self.cfg)
        self.rules = RuleEngine(self.rules_cfg)
        self.city = CityView(self.rules_cfg.get("unit_actions", []), self.cfg["trust"]["verdict_values"])
        self.decide_cfg = self.cfg["decide"]
        budget = self.decide_cfg.get("budget_s")
        if budget is None:
            budget = (llm_cfg or config.load("llm"))["budget"]["answer_s"]
        self.budget_s = float(budget)
        self.dispatch = self.rules_cfg["unit_dispatch"]
        self.ids = IdGenerator("math")
        self.rule_ids = IdGenerator("rules")
        self.tracks: dict[str, Track] = {}
        self.assigned: dict[str, Assignment] = {}             # unit_id -> incident holding it
        self._redecide: dict[str, list[str]] = {}            # incident_id -> why (failed / refused outcomes this tick)
        self.early_llm: dict[str, tuple[float, Proposal]] = {}  # LLM proposal before its incident
        self.llm_status: str | None = None
        self._lock = threading.RLock()          # paho thread (handle) and main loop (tick)

    # --- inputs -------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        wild = lambda key, ph: t[key]["topic"].replace("{" + ph + "}", "+")  # noqa: E731
        return [
            (t["meta"]["topic"], 1),
            (t["incident"]["topic"], 1),
            (t["proposal"]["topic"], 1),
            (t["outcome"]["topic"], 1),
            (t["ack"]["topic"], 1),
            (t["llm_status"]["topic"], 1),
            (wild("quarantine", "device_id"), 1),
            (wild("trust", "sensor_id"), 1),
            (wild("layer_status", "layer"), 1),
            (self.topics["external_out"]["event"]["topic"], 0),
        ]

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        with self._lock:
            self._handle(topic, payload)

    def _handle(self, topic: str, payload: bytes | str) -> None:
        t = self.topics["internal"]
        prefix = lambda key: t[key]["topic"].split("{")[0]  # noqa: E731
        try:
            if topic == self.topics["external_out"]["event"]["topic"]:
                self._on_event(payload)
                return
            msg = json.loads(payload)
            if not isinstance(msg, dict):
                return
            if topic == t["meta"]["topic"]:
                self.city.apply_meta(msg)
                self.log.info("meta_loaded", zones=len(self.city.zones), units=len(self.city.units))
            elif topic.startswith(prefix("quarantine")):
                self.city.apply_quarantine(msg)
            elif topic.startswith(prefix("trust")):
                self.city.apply_trust(msg)
            elif topic.startswith(prefix("layer_status")):
                if msg.get("layer") == "guardian" or topic.endswith("/guardian"):
                    self.city.guardian_status = msg.get("status")
            elif topic == t["llm_status"]["topic"]:
                self._on_llm_status(msg.get("status"))
            elif topic == t["ack"]["topic"]:
                self._on_ack(msg)
            elif topic == t["incident"]["topic"]:
                self.on_incident(Incident.model_validate(msg))
            elif topic == t["proposal"]["topic"]:
                self.on_proposal(Proposal.model_validate(msg))
            elif topic == t["outcome"]["topic"]:
                self.on_outcome(Outcome.model_validate(msg))
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])

    # --- incidents ------------------------------------------------------------------

    def on_incident(self, inc: Incident) -> None:
        now = self.clock()
        track = self.tracks.get(inc.incident_id)
        if inc.status == "closed":
            if track is not None:
                self._forget(track)
            return
        if track is None:
            track = Track(inc, first_seen=now, deadline=now + self.budget_s)
            self.tracks[inc.incident_id] = track
            kept = self.early_llm.pop(inc.incident_id, None)
            if kept and now - kept[0] <= float(self.decide_cfg["early_llm_keep_s"]):
                track.llm = kept[1]
            self._publish_rules(track)
            self._maybe_decide_now(track)
            return

        track.incident = inc
        self._publish_rules(track, only_if_changed=True)
        if track.state == "waiting":
            self._maybe_decide_now(track)
        elif track.state == "skipped" or self._redecide_trigger(track) or self._more_evidence(track):
            # new epoch: wait for the LLM's answer to this version again
            track.state, track.llm, track.early = "waiting", None, False
            track.deadline = now + self.budget_s
            self._maybe_decide_now(track)

    def _maybe_decide_now(self, track: Track) -> None:
        if self.llm_status == "offline":
            self._decide(track, "LLM offline")
        elif track.incident.severity in self.decide_cfg["immediate_severities"]:
            self._decide(track, f"{track.incident.severity}: rules at once", early=True)
        elif track.llm is not None:
            self._decide(track, "LLM proposal in time")

    def _more_evidence(self, track: Track) -> bool:
        if track.awaiting_evidence is None or len(track.incident.evidence) <= track.awaiting_evidence:
            return False
        track.awaiting_evidence = None
        return True

    def _redecide_trigger(self, track: Track) -> bool:
        on = self.decide_cfg["redecide_on"]
        inc = track.incident
        if on.get("severity_increase") and SEVERITY_RANK[inc.severity] > track.severity_rank:
            return True
        return bool(on.get("new_evidence_type") and not self._evidence_types(inc) <= track.evidence_types)

    def _forget(self, track: Track) -> None:
        del self.tracks[track.id]
        for uid in [u for u, a in self.assigned.items() if a.incident_id == track.id]:
            self.assigned[uid].incident_id = ""     # unit still out until the Twin says free
        self.log.info("incident_forgotten", incident_id=track.id, correlation_id=track.incident.correlation_id)

    # --- proposals ------------------------------------------------------------------

    def on_proposal(self, p: Proposal) -> None:
        if p.origin != "llm":
            return                                   # rule proposals are ours, already known
        track = self.tracks.get(p.incident_id)
        now = self.clock()
        if track is None:
            self.early_llm[p.incident_id] = (now, p)
            return
        if track.state == "waiting":
            track.llm = p
            self._decide(track, "LLM proposal in time")
        elif track.state == "decided" and track.early and now <= track.deadline:
            track.llm = p
            self._maybe_switch(track)
        else:
            self.log.info("llm_proposal_ignored", incident_id=p.incident_id, proposal_id=p.id,
                          reason=f"incident {track.state}")

    def _publish_rules(self, track: Track, only_if_changed: bool = False) -> None:
        inc = track.incident
        if not self.city.has_meta:
            return
        missing: set[str] = set()

        def pick(utype: str, params: dict[str, Any], taken: set[str]):
            action, note, short = self._allocate(track, utype, params, taken)
            if short:
                missing.add(short)
            return action, note

        actions, notes = self.rules.propose(inc, self.city, exclude=track.failed, pick_unit=pick)
        same = track.rules is not None and [a.model_dump() for a in track.rules.actions] == [
            a.model_dump() for a in actions]
        track.rules_missing = missing
        track.rules_notes = notes
        if only_if_changed and same:
            return
        p = Proposal(id=self.rule_ids.next(), timestamp=utc_now_iso(), correlation_id=inc.correlation_id,
                     incident_id=inc.incident_id, origin="rules", actions=actions,
                     justification=self.rules.justification(inc, actions, notes))
        before = [(a.target_id, a.action) for a in track.rules.actions] if track.rules else None
        track.rules = p
        spec = self.topics["internal"]["proposal"]
        self._publish(spec["topic"], p.to_json(), int(spec["qos"]), bool(spec["retain"]))
        self.log.info("rules_proposal", incident_id=inc.incident_id, correlation_id=inc.correlation_id,
                      proposal_id=p.id, actions=[a.model_dump() for a in actions], notes=notes)
        if before != [(a.target_id, a.action) for a in actions]:
            self._event("rules_proposal", f"rules proposal for {inc.type} at {inc.zone_id}: "
                        + (", ".join(f"{a.action} {a.target_id}" for a in actions) or "no action"), inc)

    # --- LLM status and give-up events ---------------------------------------------

    def _on_llm_status(self, status: str | None) -> None:
        was, self.llm_status = self.llm_status, status
        if status == "offline" and was != "offline":
            for track in [t for t in self.tracks.values() if t.state == "waiting"]:
                self._decide(track, "LLM offline")

    def _on_event(self, payload: bytes | str) -> None:
        body = self._event_body(payload)
        if body is None or body.get("source") != "brain":
            return
        if body.get("event") not in self.decide_cfg["llm_give_up_events"]:
            return
        track = self.tracks.get(body.get("incident_id") or "")
        if track is not None and track.state == "waiting":
            self._decide(track, f"LLM gave up ({body['event']})")

    def _event_body(self, payload: bytes | str) -> dict[str, Any] | None:
        """brain/event body: plain JSON, or a sealed envelope (HMAC checked when keys are given)."""
        try:
            doc = json.loads(payload)
        except ValueError:
            return None
        if isinstance(doc, dict) and "body" in doc and "v" in doc:
            try:
                env = parse_envelope(payload)
            except EnvelopeError:
                return None
            if self.seal_keys:
                tags = env.hmacs or {}
                ok = any(tags.get(layer, "").lower() == sign(env.body, key)
                         for layer, key in self.seal_keys.items())
                if not ok and env.hmac:
                    ok = any(env.hmac.lower() == sign(env.body, key) for key in self.seal_keys.values())
                if not ok:
                    self.log.warning("event_hmac_mismatch")
                    return None
            try:
                doc = json.loads(env.body)
            except ValueError:
                return None
        return doc if isinstance(doc, dict) else None

    # --- acks and outcomes ----------------------------------------------------------

    def _on_ack(self, msg: dict[str, Any]) -> None:
        change = self.city.apply_ack(msg)
        if change is None:
            return
        uid, _old, new = change
        a = self.assigned.get(uid)
        if a is None:
            return
        if new != "free":
            a.seen_busy = True
        elif a.seen_busy:
            del self.assigned[uid]
            self.log.info("unit_free", unit_id=uid)
            self._serve_waiting_units(f"{uid} free")

    def on_outcome(self, o: Outcome) -> None:
        if o.status not in ("failed", "refused"):
            return
        held = self.assigned.get(o.target_id)
        if held is not None and not held.seen_busy and held.incident_id in (o.incident_id, ""):
            del self.assigned[o.target_id]          # the dispatch never happened: the unit is not out
        track = self.tracks.get(o.incident_id)
        if track is None or o.decision_id not in track.decisions:
            return
        if o.status == "refused" and o.rule in EVIDENCE_RULES:
            # S3 / S6: not enough corroboration YET. The device is fine: do not exclude it, and do not
            # re-decide in a loop; try again when the incident gains evidence (scenarios B2, B6, B12).
            track.issued.discard((o.target_id, o.action))
            track.awaiting_evidence = len(track.incident.evidence)
            return
        track.failed.add(o.target_id)
        why = f"{o.action} {o.target_id} {o.status}" + (f" ({o.rule or o.detail})" if (o.rule or o.detail) else "")
        # coalesced: the outcomes of one decision (often several refusals at once) give ONE re-decision
        self._redecide.setdefault(track.id, []).append(why)

    def _run_redecisions(self) -> None:
        pending, self._redecide = self._redecide, {}
        for iid, whys in pending.items():
            track = self.tracks.get(iid)
            if track is None:
                continue
            why = "; ".join(whys)
            if track.redecisions >= int(self.decide_cfg["max_redecisions"]):
                self._skip(track, f"{why}; re-decision limit reached")
                continue
            track.redecisions += 1
            self._publish_rules(track)
            self._decide(track, why)

    def _serve_waiting_units(self, why: str) -> None:
        """Re-decide incidents that lacked a unit, highest priority first, if one is now free."""
        now = self.clock()
        waiting = [t for t in self.tracks.values() if t.state == "decided" and t.wants_types]
        for track in sorted(waiting, key=lambda t: -self._priority(t, now)):
            if track.redecisions >= int(self.decide_cfg["max_redecisions"]):
                continue
            p = self._priority(track, now)
            own = {uid for uid, a in self.assigned.items() if a.incident_id == track.id}
            if any(self._rival(track, uid, p, now) is None
                   for utype in track.wants_types for uid in self._free_units(track, utype, own)):
                track.redecisions += 1
                self._publish_rules(track)
                self._decide(track, why)

    # --- unit allocation ------------------------------------------------------------

    def _free_units(self, track: Track, utype: str, taken: set[str]) -> list[str]:
        out = []
        for u in self.city.units.values():
            uid = u.unit_id
            if u.type != utype or uid in taken or uid in self.city.quarantined or uid in track.failed:
                continue
            holder = self.assigned.get(uid)
            if holder is not None and holder.incident_id != track.id:
                continue
            if self.city.unit_free(uid) or (holder is not None and holder.incident_id == track.id):
                out.append(uid)
        return out

    def _allocate(self, track: Track, utype: str, params: dict[str, Any], taken: set[str],
                  preferred: str | None = None) -> tuple[ProposalAction | None, str | None, str | None]:
        """Pick a unit of `utype` for this incident (team plan 04, 6.3). No side effect.

        1. the preferred unit, else the nearest free one, not quarantined, not failed, unless a
           higher-priority undecided incident wants it (then it is left for that incident);
        2. none: a unit held by another incident whose P x preempt_ratio <= this P is recalled
           (the Twin refuses a dispatch to a busy unit); this incident is decided again with a
           dispatch once the unit is free;
        3. else nothing (the rest of the proposal is still used).
        Returns (action or None, note or None, unit type still wanted or None).
        """
        now = self.clock()
        p_self = self._priority(track, now)
        zone = track.incident.zone_id
        hops = lambda uid: self.city.target_hops(uid, zone)  # noqa: E731
        free = sorted(self._free_units(track, utype, taken),
                      key=lambda uid: (uid != preferred, 99 if hops(uid) is None else hops(uid), uid))
        notes = []
        for uid in free:
            rival = self._rival(track, uid, p_self, now)
            if rival is not None:
                notes.append(f"{uid} left for {rival.id} (P {self._priority(rival, now):.2f} > {p_self:.2f})")
                continue
            out = dict(params)
            out.setdefault(self.dispatch["zone_param"], zone)
            if preferred and uid != preferred:
                notes.append(f"{uid} instead of {preferred} (busy or unavailable)")
            return (ProposalAction(target_id=uid, action=self.dispatch["action"], params=out),
                    "; ".join(notes) or None, None)
        ratio = float(self.cfg["units"]["preempt_ratio"])
        busy = sorted((u.unit_id for u in self.city.units.values()
                       if u.type == utype and u.unit_id not in taken and u.unit_id not in self.city.quarantined
                       and u.unit_id not in track.failed and u.unit_id in self.assigned
                       and self.assigned[u.unit_id].incident_id != track.id),
                      key=lambda uid: (99 if hops(uid) is None else hops(uid), uid))
        for uid in busy:
            holder_id = self.assigned[uid].incident_id
            holder = self.tracks.get(holder_id)
            p_holder = self._priority(holder, now) if holder else 0.0
            recallable = self.city.unit_status.get(uid) in ("en_route", "on_scene")
            if recallable and p_self >= ratio * p_holder:
                notes.append(f"recall {uid} from {holder_id or 'a closed incident'} "
                             f"(P {p_self:.2f} >= {ratio:g} x {p_holder:.2f}), dispatch when free")
                return (ProposalAction(target_id=uid, action=self.dispatch["recall"], params={}),
                        "; ".join(notes), utype)
            notes.append(f"{uid} busy with {holder_id or 'a closed incident'} (P {p_holder:.2f}, "
                         f"this {p_self:.2f} < {ratio:g}x)" if p_self < ratio * p_holder
                         else f"{uid} busy, not recallable yet")
        if not notes:
            notes.append(f"no free {utype}")
        return None, "; ".join(notes), utype

    def _rival(self, track: Track, uid: str, p_self: float, now: float) -> Track | None:
        """A higher-priority open incident still waiting for a decision that wants this unit."""
        utype = self.city.units[uid].type
        best, best_p = None, p_self
        for other in self.tracks.values():
            if other is track or other.state == "skipped":
                continue
            undecided = other.state == "waiting"
            if not (undecided or utype in other.wants_types):
                continue
            named = any(a.target_id == uid for p in (other.rules, other.llm) if p for a in p.actions)
            if not (named or utype in other.wants_types):
                continue
            p_other = self._priority(other, now)
            if p_other > best_p or (p_other == best_p and best is not None and other.id < best.id):
                best, best_p = other, p_other
        return best

    # --- scoring --------------------------------------------------------------------

    def _priority(self, track: Track, now: float) -> float:
        inc = track.incident
        return self.model.priority(inc.confidence, inc.severity,
                                   self.model.zone_importance(self.city.zones.get(inc.zone_id)),
                                   now - track.first_seen)

    def _evidence_trusts(self, inc: Incident) -> list[tuple[Any, float]]:
        out = []
        for e in inc.evidence:
            t = self.city.trust.get(e.source_id, e.trust) if e.kind == "reading" else e.trust
            out.append((e, max(0.0, min(1.0, float(t)))))
        return out

    def _evidence_types(self, inc: Incident) -> frozenset[str]:
        types = set()
        for e, t in self._evidence_trusts(inc):
            if t > 0:
                types.add("call" if e.kind == "call" else (self.city.sensor_type(e.source_id) or e.source_id))
        return frozenset(types)

    def _mode(self, inc: Incident) -> str:
        sm = self.cfg["safe_mode"]
        if self.city.guardian_status in sm["guardian_states"]:
            return "safe"
        return "safe" if sm.get("unverified_incident", True) and not inc.verified else "normal"

    def _agree(self, inc: Incident) -> bool:
        sm = self.cfg["safe_mode"]
        types = {self.city.sensor_type(e.source_id) or e.source_id
                 for e, t in self._evidence_trusts(inc)
                 if e.kind == "reading" and t >= float(sm["agree_min_trust"])}
        return len(types) >= int(sm["agree_sensor_types"])

    def score(self, track: Track, p: Proposal) -> ProposalScore:
        """Check, allocate and score one proposal. Pure (no state change)."""
        now = self.clock()
        inc = track.incident
        prio = self._priority(track, now)
        trust = self.model.combine_trust([t for _, t in self._evidence_trusts(inc)])
        mode = self._mode(inc)
        agree = self._agree(inc)
        res = ProposalScore(p.id, p.origin, prio, trust, mode)
        if p.origin == "rules" and track.rules is not None and p.id == track.rules.id:
            res.missing |= track.rules_missing
            res.notes.extend(track.rules_notes)
        taken: set[str] = set()
        seen: set[tuple[str, str]] = set()
        for a in p.actions:
            tid = a.target_id
            if self.city.target_type(tid) is None:
                res.notes.append(f"dropped {tid} (unknown)")
                continue
            if not self.city.allowed(tid, a.action):
                res.notes.append(f"dropped {a.action} {tid} (not allowed)")
                continue
            if tid in self.city.quarantined:
                res.notes.append(f"dropped {tid} (quarantined)")
                continue
            if tid in track.failed:
                res.notes.append(f"dropped {tid} (failed before)")
                continue
            if self.city.is_unit(tid) and (a.action == self.dispatch["action"] or a.action == self.dispatch["recall"]):
                if (tid, a.action) in track.issued:
                    pass                                 # already sent for this incident: keep as is
                else:
                    resolved, note, short = self._allocate(track, self.city.units[tid].type,
                                                           dict(a.params), taken, preferred=tid)
                    if note:
                        res.notes.append(note)
                    if short:
                        res.missing.add(short)
                    if resolved is None:
                        continue
                    a = resolved
                tid = a.target_id
                taken.add(tid)
            key = (tid, a.action)
            if key in seen:
                continue
            seen.add(key)
            is_unit = self.city.is_unit(tid)
            s = self.model.score_action(
                priority=prio, incident_type=inc.type, target_id=tid, action=a.action,
                target_type=self.city.target_type(tid) or "", is_unit=is_unit,
                hops=self.city.target_hops(tid, inc.zone_id), trust=trust,
                safe=mode == "safe", agree=agree,
                effect_action=self.dispatch["action"] if is_unit and a.action == self.dispatch["recall"] else None)
            if s.score <= 0:
                res.notes.append(f"dropped {a.action} {tid} (score {s.score:.2f})")
                res.breakdown.append(s)
                continue
            res.breakdown.append(s)
            res.actions.append(a)
            res.score += s.score
        return res

    # --- deciding -------------------------------------------------------------------

    def _decide(self, track: Track, why: str, early: bool = False) -> None:
        inc = track.incident
        if not self.city.has_meta:
            self._skip(track, "no city description yet")
            return
        trusts = self._evidence_trusts(inc)
        if trusts and all(t == 0 for _, t in trusts):
            self._skip(track, "all evidence has trust 0 (rejected by Guardian)")
            return
        self._publish_rules(track, only_if_changed=True)     # units may have changed while waiting
        scored = [self.score(track, p) for p in (track.rules, track.llm) if p is not None]
        if not scored:
            self._skip(track, "no proposal")
            return
        # best score; on a tie the rule proposal (known, predictable behaviour)
        scored.sort(key=lambda s: (-s.score, s.origin != "rules"))
        best = scored[0]
        if best.score <= float(self.decide_cfg["min_score"]):
            self._skip(track, f"best score {best.score:.2f} <= {float(self.decide_cfg['min_score']):g}", best)
            return
        new = [a for a in best.actions if (a.target_id, a.action) not in track.issued]
        if not new:
            if track.decisions:
                self._skip(track, f"no new action after: {why}", best, keep_state=True)
            else:
                self._skip(track, "no action", best)
            return
        self._publish_decision(track, best, scored[1:], new, why, early)

    def _maybe_switch(self, track: Track) -> None:
        """Critical incident decided on rules: switch if the LLM proposal is better."""
        llm = self.score(track, track.llm)
        current = self.score(track, track.rules) if track.rules else None
        base = max(track.score, current.score if current else 0.0)
        if llm.score <= base + float(self.decide_cfg["switch_margin"]):
            self.log.info("llm_proposal_not_better", incident_id=track.id, llm=round(llm.score, 3),
                          current=round(base, 3))
            return
        new = [a for a in llm.actions if (a.target_id, a.action) not in track.issued]
        if not new:
            return
        self._publish_decision(track, llm, [current] if current else [], new,
                               "better LLM proposal", early=True)

    def _publish_decision(self, track: Track, best: ProposalScore, others: list[ProposalScore],
                          actions: list[ProposalAction], why: str, early: bool) -> None:
        inc = track.incident
        changed = bool(track.decisions)
        risk = max((s.risk for s in best.breakdown if (s.target_id, s.action) in
                    {(a.target_id, a.action) for a in actions}), default=0.0)
        evidence = [e.source_id for e, t in self._evidence_trusts(inc) if e.kind == "reading" and t > 0]
        explanation = self._explain(best, others, actions, why if changed else None)
        d = Decision(
            id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=inc.correlation_id,
            incident_id=inc.incident_id, chosen_proposal=best.proposal_id, origin=best.origin,  # type: ignore[arg-type]
            actions=actions, score=round(best.score, 3),
            alternatives=[DecisionAlternative(proposal_id=o.proposal_id, origin=o.origin,  # type: ignore[arg-type]
                                              score=round(o.score, 3)) for o in others],
            risk=self.model.risk_level(risk), evidence_sensors=evidence, mode=best.mode,  # type: ignore[arg-type]
            explanation=explanation,
        )
        spec = self.topics["internal"]["decision"]
        self._publish(spec["topic"], d.to_json(), int(spec["qos"]), bool(spec["retain"]))
        # state
        track.state, track.score = "decided", best.score
        track.early = track.early or early
        track.decisions.append(d.id)
        track.severity_rank = SEVERITY_RANK[inc.severity]
        track.evidence_types = self._evidence_types(inc)
        track.skip_reason = None
        for a in actions:
            track.issued.add((a.target_id, a.action))
            if self.city.is_unit(a.target_id) and a.action == self.dispatch["action"]:
                self.assigned[a.target_id] = Assignment(inc.incident_id, since=self.clock())
        got = {self.city.units[a.target_id].type for a in actions
               if self.city.is_unit(a.target_id) and a.action == self.dispatch["action"]}
        track.wants_types = best.missing - got
        self.log.info("decision", incident_id=inc.incident_id, correlation_id=inc.correlation_id,
                      decision_id=d.id, chosen=best.proposal_id, origin=best.origin, why=why,
                      priority=round(best.priority, 4), trust=round(best.trust, 4), mode=best.mode,
                      score=round(best.score, 4), breakdown=[s.as_dict() for s in best.breakdown],
                      alternatives={o.proposal_id: round(o.score, 4) for o in others}, notes=best.notes)
        self._event("decision_changed" if changed else "decision", explanation, inc)
        if not changed:
            self._serve_waiting_units("a decision left a unit free")

    def _explain(self, best: ProposalScore, others: list[ProposalScore], actions: list[ProposalAction],
                 change: str | None) -> str:
        acts = ", ".join(f"{a.action} {a.target_id}" + (f" to {a.params[self.dispatch['zone_param']]}"
                         if a.action == self.dispatch["action"] and self.dispatch["zone_param"] in a.params else "")
                         for a in actions)
        over = (" chosen over " + ", ".join(f"{o.origin} ({o.score:.2f})" for o in others)) if others else ""
        text = f"{best.origin} proposal (score {best.score:.2f}){over}: {acts}"
        if best.mode == "safe":
            text += " [safe mode]"
        if best.notes:
            text += "; " + "; ".join(best.notes)
        if change:
            text = f"changed ({change}): " + text
        return text[: int(self.decide_cfg["explanation_max_chars"])]

    def _skip(self, track: Track, reason: str, best: ProposalScore | None = None,
              keep_state: bool = False) -> None:
        inc = track.incident
        if not keep_state:
            track.state = "skipped" if not track.decisions else "decided"
        if track.skip_reason == reason:
            return
        track.skip_reason = reason
        self.log.info("decision_skipped", incident_id=inc.incident_id, correlation_id=inc.correlation_id,
                      reason=reason, breakdown=[s.as_dict() for s in best.breakdown] if best else None,
                      notes=best.notes if best else None)
        self._event("decision_skipped", f"no decision for {inc.type} at {inc.zone_id}: {reason}"
                    + ("; " + "; ".join(best.notes) if best and best.notes else ""), inc)

    # --- time -----------------------------------------------------------------------

    def tick(self) -> None:
        with self._lock:
            self._tick()

    def _tick(self) -> None:
        now = self.clock()
        self._run_redecisions()
        ttl = float(self.decide_cfg.get("reservation_ttl_s", 30))
        for uid in [u for u, a in self.assigned.items() if not a.seen_busy and now - a.since > ttl]:
            # dispatched but never reported out (no ack, Twin reset): do not hold the unit forever
            del self.assigned[uid]
            self.log.info("reservation_released", unit_id=uid, after_s=ttl)
            self._serve_waiting_units(f"{uid} reservation expired")
        for track in [t for t in self.tracks.values() if t.state == "waiting" and now >= t.deadline]:
            self._decide(track, f"LLM budget of {self.budget_s:g} s used up")
        keep = float(self.decide_cfg["early_llm_keep_s"])
        for iid in [i for i, (t, _) in self.early_llm.items() if now - t > keep]:
            del self.early_llm[iid]

    # --- helpers --------------------------------------------------------------------

    def _event(self, event: str, summary: str, inc: Incident | None = None) -> None:
        publish_event(self._publish, self.topics, self.ids, self.seal_keys, event, summary,
                      correlation_id=inc.correlation_id if inc else None,
                      incident_id=inc.incident_id if inc else None)
