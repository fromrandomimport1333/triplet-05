"""Detection service: readings and calls in, incidents out (team doc 02-graph, section 4).

Independent of MQTT, like the other services: `handle(topic, payload, retained)` takes the
internal messages and the service calls `publish`. `tick()` runs a check every second.

Step 1, per sensor (sensors.py): abnormal or not, with a frozen baseline while abnormal.

Step 2, per zone and incident type: the signals of config/detection.yaml `incidents`.
A *source* is one sensor, or all recent calls of one category in the zone (so ten calls
from one zone are one source, scenario B7). Each source i gets

    p_i = min(cap_i, weight_i * strength_i * trust_i)
    confidence = 1 - prod_i (1 - p_i)              (noisy-OR: independent sources agree)

- strength: 1.0 for a reading at alert level, else min(1, z / z_full); calls: min(1, n / full_count)
- trust (step 3): Guardian's verdict, trusted = trust_score or 1.0, suspicious = trust_score
  or 0.3, rejected = 0 (source ignored), no verdict or Guardian offline = 0.6 (verified false);
  calls use `calls.trust`
- cap: cap_single (0.6) for a reading, cap_alert (0.95) when it is at alert level for
  `consecutive` readings, calls.cap (0.55) for calls: one source alone stays below a
  configurable confidence unless it is a sustained alert-level reading
- support signals (power spike for fire, density for a jam, the same sensor type in a
  neighbouring zone x neighbour_factor) only count when a primary signal is present
- gates: flood needs the water level rising to open (not to stay open) unless a flood call
  confirms it; an accident speed signal needs a sudden drop, and an accident without any call
  stays at max_without_call (below the open threshold: a drop alone may be a jam); a jam needs
  the speed low for min_abnormal_s and is ignored during a city-wide pattern (rush hour: most
  zones slow together) unless the zone is far slower than the city median; a jam is suppressed
  by an accident in the zone, a power problem by a fire in the zone or a neighbour

Step 4, lifecycle per zone + type (one open incident at most): opened after open_checks checks
in a row at or above open_threshold; updated on a new evidence source or a severity change (at
most every min_update_interval_s, a lower severity only after downgrade_after_s); closed after
close_checks checks below close_threshold or with no live evidence. Severity: low below
medium_confidence; high with a margin over alert >= high_margin or high_sources agreeing
sources at high_confidence; critical only with critical_sources sources, critical_confidence
and critical_margin together.
"""

from __future__ import annotations

import json
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..common import config
from ..common.events import publish_event
from ..common.ids import IdGenerator
from ..common.log import EventLogger, utc_now_iso
from ..common.models import SEVERITY_RANK, Incident, IncidentEvidence
from .sensors import SensorState

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain



@dataclass(slots=True)
class Call:
    at: float
    call_id: str
    zone_id: str
    category: str
    corr: str


@dataclass(slots=True)
class Source:
    key: str                     # sensor id, "calls:<category>" or "case:<zone>"
    kind: str                    # reading | call | case (case memory: derived from the readings, not evidence)
    role: str                    # primary | support
    p: float
    evidence: list[IncidentEvidence]
    since: float                 # when the signal started (chain start)
    corr: str | None
    desc: str
    verified: bool = True
    margin: float = 0.0


@dataclass(slots=True)
class Assessment:
    zone_id: str
    type: str
    confidence: float = 0.0
    sources: list[Source] = field(default_factory=list)
    note: str | None = None      # why the confidence is held down (suppressed, ...)
    rejected: list[str] = field(default_factory=list)   # abnormal primary sensors Guardian rejected
    case: str | None = None      # the case memory's line, when it changed the assessment

    @property
    def keys(self) -> set[str]:
        return {s.key for s in self.sources}


@dataclass(slots=True)
class OpenIncident:
    incident_id: str
    correlation_id: str
    severity: str
    verified: bool
    keys: set[str]
    last_update: float
    lower_since: float | None = None
    last: Assessment | None = None


@dataclass(slots=True)
class Track:
    open_streak: int = 0
    close_streak: int = 0
    incident: OpenIncident | None = None
    announced: set[str] = field(default_factory=set)   # rejected sensors already on the timeline
    hold_since: float | None = None                     # opening held for the case memory since


class DetectionService:
    def __init__(
        self,
        publish: Publisher,
        *,
        clock: Callable[[], float],
        logger: EventLogger | None = None,
        det_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        seal_keys: dict[str, bytes] | None = None,
    ):
        self.cfg = det_cfg or config.load("detection")
        self.topics = topics_cfg or config.load("topics")
        self._publish = publish
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-detection", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.seal_keys = seal_keys
        self.ids = IdGenerator("det")
        self.incident_ids = IdGenerator("inc", width=4)
        self.scfg = self.cfg["sensor"]
        # city (from meta)
        self.zones: dict[str, dict[str, Any]] = {}          # zone_id -> {name, adjacent}
        self.decls: dict[str, dict[str, Any]] = {}          # sensor_id -> declaration
        # live state
        self.sensors: dict[str, SensorState] = {}
        self.trust: dict[str, tuple[str, float | None]] = {}   # sensor_id -> (verdict, score)
        self.guardian_online = True
        self.twin_status: str | None = None
        self.calls: deque[Call] = deque(maxlen=2000)        # a flood call counts for an hour (calls.by_category)
        self.tracks: dict[tuple[str, str], Track] = {}
        self.cases: dict[str, dict[str, Any]] = {}          # zone -> newest case memory verdict (+ "_at")
        self._citywide_until = float("-inf")
        self._citywide_median = float("inf")

    # --- inputs -------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        return [
            (t["meta"]["topic"], 1),
            (t["reading"]["topic"], 0),
            (t["call"]["topic"], 1),
            (t["trust"]["topic"].replace("{sensor_id}", "+"), 1),
            (t["layer_status"]["topic"].replace("{layer}", "+"), 1),
        ] + ([(t["case"]["topic"], 1)] if "case" in t else [])

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
            if topic == t["reading"]["topic"]:
                self.on_reading(msg)
            elif topic == t["call"]["topic"]:
                self.on_call(msg)
            elif topic.startswith(_prefix(t["trust"]["topic"])):
                self.trust[msg["sensor_id"]] = (msg["verdict"], msg.get("trust_score"))
            elif topic.startswith(_prefix(t["layer_status"]["topic"])):
                if msg.get("layer") == "guardian":
                    self.guardian_online = msg.get("status") != "offline"
                elif msg.get("layer") == "twin":
                    was, self.twin_status = self.twin_status, msg.get("status")
                    if was == "offline" and self.twin_status != "offline":
                        self.twin_restarted()
            elif topic == t["meta"]["topic"]:
                self.on_meta(msg)
            elif "case" in t and topic == t["case"]["topic"]:
                self.cases[msg["zone_id"]] = {**msg, "_at": self.clock()}
                self.check()
        except (KeyError, TypeError, ValueError) as e:
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])

    def on_meta(self, meta: dict[str, Any]) -> None:
        zones: dict[str, dict[str, Any]] = {}
        for z in meta["zones"]:
            zones[z["zone_id"]] = {"name": z.get("name"), "adjacent": set(z.get("adjacent") or [])}
        for zid, z in zones.items():          # adjacency is symmetric
            for other in list(z["adjacent"]):
                if other in zones:
                    zones[other]["adjacent"].add(zid)
        self.zones = zones
        self.decls = {s["sensor_id"]: dict(s) for s in meta["sensors"]}
        for sid, st in self.sensors.items():
            d = self.decls.get(sid)
            if d:
                st.alert = _num(d.get("alert"))
                st.zone_id = d["zone_id"]
        self.log.info("meta_loaded", zones=len(zones), sensors=len(self.decls))

    def on_reading(self, msg: dict[str, Any]) -> None:
        sid = msg["sensor_id"]
        st = self.sensors.get(sid)
        if st is None:
            d = self.decls.get(sid, {})
            st = SensorState(
                sensor_id=sid, zone_id=d.get("zone_id", msg["zone_id"]), type=d.get("type", msg["type"]),
                unit=d.get("unit", msg.get("unit", "")), alert=_num(d.get("alert")),
                direction=self.scfg["direction"].get(d.get("type", msg["type"]), self.scfg["default_direction"]),
                history=deque(maxlen=int(self.scfg["history"])),
            )
            self.sensors[sid] = st
        ts = _event_time(msg.get("timestamp"), self.clock())
        if st.last_ts is not None and ts < st.last_ts:
            return                                    # out of order: a newer one is already counted
        verdict = self.trust.get(sid, (None, None))[0]
        changed = st.update(float(msg["value"]), ts, msg.get("correlation_id"), self.scfg,
                            learn=verdict != "rejected")
        if changed:
            self.log.info("sensor_abnormal" if st.abnormal else "sensor_normal", sensor_id=sid,
                          zone_id=st.zone_id, value=st.last_value, z=round(st.z, 2),
                          correlation_id=msg.get("correlation_id"))
            self.check()

    def on_call(self, msg: dict[str, Any]) -> None:
        if any(c.call_id == msg["call_id"] for c in self.calls):
            return                                   # redelivered (QoS 1): one citizen call counts once
        self.calls.append(Call(self.clock(), msg["call_id"], msg["zone_id"], msg.get("category", "other"),
                               msg.get("correlation_id") or msg.get("id", "")))
        self.check()

    def tick(self) -> None:
        self.check()

    # --- step 3: trust -------------------------------------------------------------

    def weight(self, sensor_id: str) -> tuple[float, bool]:
        """(weight, verified) of a sensor's readings, 02-graph 4.3."""
        t = self.cfg["trust"]
        if not self.guardian_online or sensor_id not in self.trust:
            return float(t["no_verdict"]), False
        verdict, score = self.trust[sensor_id]
        if verdict == "rejected":
            return float(t["rejected"]), True
        if verdict == "suspicious":
            return float(score if score is not None else t["suspicious_default"]), True
        return float(score if score is not None else t["trusted_default"]), True

    # --- step 2: combine -----------------------------------------------------------

    def _zone_sensors(self, zone: str, stype: str) -> list[SensorState]:
        return [s for s in self.sensors.values() if s.zone_id == zone and s.type == stype]

    def _citywide(self, now: float) -> tuple[bool, float | None]:
        c = self.cfg["citywide"]
        stype = c["sensor_type"]
        stale_s = float(self.scfg["stale_s"])
        live = [s for s in self.sensors.values() if s.type == stype and not s.stale(now, stale_s)]
        zones = {s.zone_id for s in live}
        if not zones:
            return False, None
        degraded = {s.zone_id for s in live if s.abnormal}
        median = statistics.median(s.last_value for s in live)
        if len(degraded) / len(zones) >= float(c["fraction"]):
            self._citywide_until = now + float(c["hold_s"])
            self._citywide_median = median
        if now <= self._citywide_until:
            # while the pattern fades out zone by zone, compare with the median seen during it
            return True, min(median, self._citywide_median)
        return False, median

    def assess(self, zone: str, itype: str, now: float, is_open: bool,
               citywide: tuple[bool, float | None]) -> Assessment:
        spec = self.cfg["incidents"][itype]
        comb = self.cfg["combine"]
        stale_s = float(self.scfg["stale_s"])
        a = Assessment(zone, itype)
        calls = self._call_sources(zone, spec.get("calls") or {}, now, self._cleared_at(zone, spec, now))

        def reading_source(st: SensorState, rule: dict[str, Any], role: str, factor: float) -> Source | None:
            if st.stale(now, stale_s) or not st.abnormal:
                return None
            if rule.get("require_rising") and not is_open and not calls and not st.rising(self.scfg):
                return None
            if rule.get("require_sudden") and not st.sudden:
                return None
            if rule.get("min_drop_frac") and st.drop_frac() < float(rule["min_drop_frac"]):
                return None
            min_s = rule.get("min_abnormal_s")
            if min_s and (st.abnormal_since is None or now - st.abnormal_since < float(min_s)):
                return None
            if rule.get("citywide_check") and citywide[0] and citywide[1] is not None:
                if st.last_value >= citywide[1] * float(self.cfg["citywide"]["outlier_ratio"]):
                    return None
            w, verified = self.weight(st.sensor_id)
            if w <= 0:                            # rejected by Guardian: ignored
                if role == "primary" and factor == 1.0:
                    a.rejected.append(st.sensor_id)
                return None
            cap = float(comb["cap_alert"]) if st.alert_sustained(self.scfg) and factor == 1.0 \
                else float(comb["cap_single"])
            p = min(cap, float(rule["weight"]) * factor * st.strength(self.scfg) * w)
            desc = _reading_desc(st, rising=bool(rule.get("require_rising")) and st.rising(self.scfg))
            return Source(st.sensor_id, "reading", role, p,
                          [IncidentEvidence(source_id=st.sensor_id, kind="reading", value=round(st.last_value, 3),
                                            unit=st.unit, trust=round(w, 2))],
                          st.onset_ts or st.abnormal_since or now, st.onset_corr or st.last_corr, desc,
                          verified, st.margin())

        for stype, rule in (spec.get("sensors") or {}).items():
            for st in self._zone_sensors(zone, stype):
                s = reading_source(st, rule, rule.get("role", "primary"), 1.0)
                if s:
                    a.sources.append(s)
        for stype in spec.get("neighbours") or []:
            rule = spec["sensors"][stype]
            for nz in sorted(self.zones.get(zone, {}).get("adjacent", ())):
                for st in self._zone_sensors(nz, stype):
                    s = reading_source(st, rule, "support", float(comb["neighbour_factor"]))
                    if s:
                        s.desc += f" (neighbour {nz})"
                        a.sources.append(s)
        self._apply_case(a, spec.get("memory"), now)
        a.sources.extend(calls)

        min_p = float(comb["min_source"])
        a.sources = [s for s in a.sources if s.p >= min_p]
        if not any(s.role == "primary" for s in a.sources):
            a.sources, a.confidence = [], 0.0
            return a
        rest = 1.0
        for s in a.sources:
            rest *= 1.0 - s.p
        a.confidence = 1.0 - rest
        cap = spec.get("max_without_call")
        if cap is not None and not calls and a.confidence > float(cap):
            a.confidence = float(cap)
            a.note = "no call confirms it"
        a.sources.sort(key=lambda s: -s.p)
        return a

    def _apply_case(self, a: Assessment, mem: dict[str, Any] | None, now: float) -> None:
        """The case memory's verdict for the zone (src/memory), if fresh and clear enough.

        fire: one more *support* source (it never opens an incident alone, and never counts as an
        independent source for severity or the safety rules: it is derived from the same sensors).
        nuisance (e.g. cooking): the zone's reading sources are held down by `nuisance_factor`;
        residents' calls still count in full (they are added after this)."""
        case = self.cases.get(a.zone_id)
        if not mem or not case or case.get("incident_type") != a.type                 or now - case["_at"] > float(mem.get("max_age_s", 20)) or float(case["share"]) < float(mem["min_share"]):
            return
        verdict, n, k = case["verdict"], int(case["votes"].get(case["verdict"], 0)), int(case["k"])
        if verdict == "nuisance":
            for src in a.sources:
                src.p *= float(mem["nuisance_factor"])
            a.case = case.get("summary")
            a.note = f"smoke looks like cooking ({n} of {k} similar lab cases)"
        elif verdict == "fire":
            p = min(float(self.cfg["combine"]["cap_single"]), float(mem["fire_weight"]) * float(case["share"]))
            a.sources.append(Source(f"case:{a.zone_id}", "case", "support", p, [], now, case.get("correlation_id"),
                                    f"case memory: {n} of {k} similar lab cases are fires"))
            a.case = case.get("summary")

    def _held_for_case(self, tr: Track, a: Assessment, now: float) -> bool:
        """Readings alone in a zone with the memory's sensors (smoke, CO): wait up to memory.wait_s for
        its verdict before opening, so burnt toast is not a fire. A call, or a verdict, ends the wait."""
        mem = self.cfg["incidents"][a.type].get("memory") or {}
        wait = float(mem.get("wait_s", 0))
        types = set(mem.get("wait_for") or [])
        if not wait or a.case is not None or any(s.kind == "call" for s in a.sources)                 or not any(d.get("zone_id") == a.zone_id and d.get("type") in types for d in self.decls.values()):
            return False
        if tr.hold_since is None:
            tr.hold_since = now
            self.log.info("incident_held_for_case_memory", zone_id=a.zone_id, type=a.type,
                          confidence=round(a.confidence, 2), wait_s=wait)
        return now - tr.hold_since < wait

    def _cleared_at(self, zone: str, spec: dict[str, Any], now: float) -> float | None:
        """When live, trusted primary sensors of the zone reported it back to normal.

        Calls made before that no longer count (config calls.superseded_by_recovery): a resident's
        report from two minutes ago must not keep a flood open once the water sensor, newer and
        trusted, says it is over. None if any primary sensor is still abnormal, or none can tell.
        """
        if not self.cfg["calls"].get("superseded_by_recovery", True):
            return None
        stale_s = float(self.scfg["stale_s"])
        cleared = None
        for stype, rule in (spec.get("sensors") or {}).items():
            if rule.get("role", "primary") != "primary":
                continue
            for st in self._zone_sensors(zone, stype):
                if st.stale(now, stale_s) or self.weight(st.sensor_id)[0] <= 0:
                    continue                      # silent or rejected: cannot contradict a call
                if st.abnormal:
                    return None
                if st.recovered_at is not None:
                    cleared = st.recovered_at if cleared is None else max(cleared, st.recovered_at)
        return cleared

    def _call_sources(self, zone: str, cats: dict[str, float], now: float,
                      not_before: float | None = None) -> list[Source]:
        c = self.cfg["calls"]
        out = []
        for cat, weight in cats.items():
            per = (c.get("by_category") or {}).get(cat) or {}     # e.g. flood: calibrated on NYC 311 (Ida)
            window = float(per.get("window_s", c["window_s"]))
            recent = [k for k in self.calls if k.zone_id == zone and k.category == cat and now - k.at <= window
                      and (not_before is None or k.at > not_before)]
            if not recent:
                continue
            strength = min(1.0, len(recent) / float(per.get("full_count", c["full_count"])))
            trust = float(c["trust"])
            p = min(float(c["cap"]), float(weight) * strength * trust)
            ev = [IncidentEvidence(source_id=k.call_id, kind="call", category=cat, trust=round(trust, 2))
                  for k in recent[-int(c["max_evidence"]):]]
            n = len(recent)
            out.append(Source(f"calls:{cat}", "call", "primary", p, ev, recent[0].at, recent[0].corr,
                              f"{n} {cat} call{'s' if n > 1 else ''} in {_span(window)}"))
        return out

    def severity(self, a: Assessment) -> str:
        s = self.cfg["severity"]
        n = sum(1 for src in a.sources if src.kind != "case")   # the case memory is not another source
        margin = max((src.margin for src in a.sources if src.role == "primary"), default=0.0)
        conf = a.confidence
        if conf >= float(s["critical_confidence"]) and n >= int(s["critical_sources"]) \
                and margin >= float(s["critical_margin"]):
            return "critical"
        if margin >= float(s["high_margin"]) or (n >= int(s["high_sources"]) and conf >= float(s["high_confidence"])):
            return "high"
        if conf >= float(s["medium_confidence"]):
            return "medium"
        return "low"

    # --- step 4: lifecycle -----------------------------------------------------------

    def check(self) -> None:
        """Evaluate every zone and incident type once; open / update / close incidents."""
        if not self.zones:
            return
        now = self.clock()
        citywide = self._citywide(now)
        types = list(self.cfg["incidents"])
        found: dict[tuple[str, str], Assessment] = {}
        for zone in self.zones:
            for itype in types:
                tr = self.tracks.get((zone, itype))
                found[(zone, itype)] = self.assess(zone, itype, now, bool(tr and tr.incident), citywide)
        self._suppress(found)
        lc = self.cfg["lifecycle"]
        for key, a in found.items():
            tr = self.tracks.setdefault(key, Track())
            inc = tr.incident
            if inc is None:
                self._announce_rejected(tr, a)
                tr.open_streak = tr.open_streak + 1 if a.confidence >= float(lc["open_threshold"]) else 0
                if tr.open_streak == 0:
                    tr.hold_since = None
                if tr.open_streak >= int(lc["open_checks"]) and not self._held_for_case(tr, a, now):
                    self._open(tr, a, now)
                continue
            if a.confidence < float(lc["close_threshold"]) or not a.sources:
                tr.close_streak += 1
            else:
                tr.close_streak = 0
            if tr.close_streak >= int(lc["close_checks"]):
                self._close(tr, a)
            else:
                self._maybe_update(tr, a, now)

    def _announce_rejected(self, tr: Track, a: Assessment) -> None:
        """B3: say once on the timeline that an abnormal sensor was ignored (Guardian rejected it)."""
        if not a.rejected:
            tr.announced.clear()
            return
        for sid in a.rejected:
            if sid in tr.announced:
                continue
            tr.announced.add(sid)
            st = self.sensors[sid]
            where = self._where(a.zone_id)
            summary = (f"{a.type} at {where} not opened: {sid} abnormal ({st.last_value:.3g} {st.unit}) "
                       "but rejected by Guardian")
            self.log.info("evidence_rejected", sensor_id=sid, zone_id=a.zone_id, type=a.type,
                          correlation_id=st.onset_corr or st.last_corr)
            self._event("evidence_rejected", summary.strip()[:300], st.onset_corr or st.last_corr, None)

    def _suppress(self, found: dict[tuple[str, str], Assessment]) -> None:
        open_th = float(self.cfg["lifecycle"]["open_threshold"])

        # confidences BEFORE any suppression: the result must not depend on the order of the loop
        before = {key: a.confidence for key, a in found.items()}

        def explains(zone: str, other: str) -> bool:
            tr = self.tracks.get((zone, other))
            return bool(tr and tr.incident) or before.get((zone, other), 0.0) >= open_th

        for (zone, itype), a in found.items():
            spec = self.cfg["incidents"][itype]
            if not spec.get("suppressed_by") or a.confidence == 0:
                continue
            area = [zone]
            if spec.get("suppress_in_neighbours"):
                area += sorted(self.zones.get(zone, {}).get("adjacent", ()))
            hit = next(((z, o) for z in area for o in spec["suppressed_by"] if explains(z, o)), None)
            if hit:
                a.confidence, a.sources, a.note = 0.0, [], f"explained by {hit[1]} at {hit[0]}"

    def _open(self, tr: Track, a: Assessment, now: float) -> None:
        primaries = [s for s in a.sources if s.role == "primary"] or a.sources
        start = min(primaries, key=lambda s: s.since)
        inc = OpenIncident(
            incident_id=self.incident_ids.next(),
            correlation_id=start.corr or self.ids.next(),
            severity=self.severity(a),
            verified=self._verified(a),
            keys=a.keys,
            last_update=now,
            last=a,
        )
        tr.incident, tr.open_streak, tr.close_streak = inc, 0, 0
        reason = "; ".join(s.desc for s in a.sources)
        self._emit(inc, a, "opened", reason)

    def _maybe_update(self, tr: Track, a: Assessment, now: float) -> None:
        inc = tr.incident
        assert inc is not None
        inc.last = a
        wanted = self.severity(a)
        rank, cur = SEVERITY_RANK[wanted], SEVERITY_RANK[inc.severity]
        target = inc.severity
        if rank > cur:
            target, inc.lower_since = wanted, None
        elif rank < cur:
            inc.lower_since = inc.lower_since if inc.lower_since is not None else now
            if now - inc.lower_since >= float(self.cfg["lifecycle"]["downgrade_after_s"]):
                target = wanted
        else:
            inc.lower_since = None
        new_keys = a.keys - inc.keys
        if not new_keys and target == inc.severity:
            return
        if now - inc.last_update < float(self.cfg["lifecycle"]["min_update_interval_s"]):
            return                                        # rate limit: published at a later check
        parts = []
        if new_keys:
            parts.append("new evidence: " + "; ".join(s.desc for s in a.sources if s.key in new_keys))
        if target != inc.severity:
            parts.append(f"severity {inc.severity} -> {target}")
        inc.keys |= new_keys
        inc.severity = target
        inc.lower_since = None
        inc.last_update = now
        inc.verified = self._verified(a)
        self._emit(inc, a, "updated", "; ".join(parts))

    def twin_restarted(self) -> None:
        """The Twin came back after being offline (a restart or a scenario reset): its city state was
        reset, so what was learned about the current situation is stale. Open incidents are closed
        with that reason, recent calls and sensor episodes are forgotten; baselines are kept."""
        if not self.cfg.get("lifecycle", {}).get("reset_on_twin_restart", True):
            return
        for (zone, itype), tr in self.tracks.items():
            if tr.incident is not None:
                self._emit(tr.incident, Assessment(zone, itype), "closed", "Twin restarted: its city state was reset")
        self.tracks.clear()
        self.calls.clear()
        for st in self.sensors.values():
            st.reset_episode()
        self.log.info("twin_restarted", note="incidents closed, calls and sensor episodes forgotten")

    def _close(self, tr: Track, a: Assessment) -> None:
        inc = tr.incident
        assert inc is not None
        lc = self.cfg["lifecycle"]
        if not a.sources:
            reason = "no live evidence left" + (f" ({a.note})" if a.note else "")
        else:
            reason = f"confidence {a.confidence:.2f} below {float(lc['close_threshold']):g}"
        self._emit(inc, a, "closed", reason)
        tr.incident, tr.close_streak, tr.open_streak = None, 0, 0

    def _verified(self, a: Assessment) -> bool:
        readings = [s for s in a.sources if s.kind == "reading"]
        return self.guardian_online and bool(readings) and all(s.verified for s in readings)

    # --- publishing ----------------------------------------------------------------

    def _emit(self, inc: OpenIncident, a: Assessment, status: str, reason: str) -> None:
        max_ev = int(self.cfg["combine"]["max_evidence"])
        evidence = [e for s in a.sources for e in s.evidence][:max_ev]
        msg = Incident(
            id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=inc.correlation_id,
            incident_id=inc.incident_id, type=a.type, zone_id=a.zone_id, status=status,
            confidence=round(a.confidence, 2), severity=inc.severity, verified=inc.verified,
            evidence=evidence, reason=reason[:300] or None, case=(a.case or "")[:300] or None,
        )
        spec = self.topics["internal"]["incident"]
        self._publish(spec["topic"], msg.to_json(), int(spec["qos"]), bool(spec["retain"]))
        self.log.info(f"incident_{status}", incident_id=inc.incident_id, correlation_id=inc.correlation_id,
                      type=a.type, zone_id=a.zone_id, confidence=msg.confidence, severity=inc.severity,
                      verified=inc.verified, reason=reason)
        where = self._where(a.zone_id)
        if status == "closed":
            summary = f"{a.type} closed at {where} (confidence {a.confidence:.2f}): {reason}"
        else:
            summary = f"{a.type} {status} at {where} (confidence {a.confidence:.2f}, {inc.severity})"
            if not inc.verified:
                summary += ", not verified by Guardian"
            if status == "updated":
                summary += f": {reason}"
        self._event(f"incident_{status}", summary[:300], inc.correlation_id, inc.incident_id)

    def _where(self, zone_id: str) -> str:
        name = self.zones.get(zone_id, {}).get("name")
        return f"{zone_id} {name}" if name else zone_id

    def _event(self, event: str, summary: str, correlation_id: str | None, incident_id: str | None) -> None:
        publish_event(self._publish, self.topics, self.ids, self.seal_keys, event, summary,
                      correlation_id=correlation_id, incident_id=incident_id)

    # --- introspection (tests, logs) -------------------------------------------------

    def open_incidents(self) -> dict[tuple[str, str], OpenIncident]:
        return {k: t.incident for k, t in self.tracks.items() if t.incident is not None}


# --- helpers -------------------------------------------------------------------------------


def _span(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds >= 600 else f"{seconds:g} s"


def _prefix(template: str) -> str:
    return template.split("{")[0]


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _event_time(text: Any, fallback: float) -> float:
    """Event timestamp (ISO 8601) as epoch seconds, or `fallback` if absent or unreadable.
    Never later than `fallback` (a Twin clock ahead of ours must not make data look fresh)."""
    if not isinstance(text, str):
        return fallback
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback
    return min(ts, fallback)


def _reading_desc(st: SensorState, rising: bool) -> str:
    v = f"{st.last_value:.3g} {st.unit}".strip()
    if st.last_value is not None and st.at_alert(st.last_value):
        cmp = "<=" if st.direction == "down" else ">="
        text = f"{st.sensor_id} at alert level ({v} {cmp} {st.alert:g})"
    else:
        text = f"{st.sensor_id} abnormal ({v}, z {st.z:.1f})"
    if rising:
        text += ", rising"
    if st.sudden:
        text += ", sudden drop"
    return text
