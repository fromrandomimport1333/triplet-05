"""Guardian detection pipeline (Phase 1 order, each step short-circuits):

 1. schema + Twin signature   -> invalid  => rejected
 2. replay (event_id / seq)   -> replay   => rejected
 3. plausibility              -> fails    => rejected
 4. EWMA z-score anomaly score (+ stuck-sensor cap)
 5. score -> verdict (>=0.8 trusted | 0.4-0.8 suspicious | <0.4 rejected)
 6. sign verdict, persist, publish guardian/verdicts/<device_id>, audit_log (hash-chained)
 + containment: N consecutive counted rejects => auto-quarantine.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from ..core.events import (VERDICT_SCHEMA_VERSION, VerdictSigner, check_schema, iso,
                           parse_ts, utcnow, verify_event_signature)
from ..db.session import VerdictRow
from .anomaly import EwmaAnomalyDetector, map_score_to_verdict
from .plausibility import PlausibilityChecker
from .replay_guard import ReplayGuard

UNATTRIBUTED = "_unattributed"


@dataclass
class StepResult:
    step: int
    verdict: str | None
    score: float
    reason: str
    counts_for_quarantine: bool = True


@dataclass
class DetectionPipeline:
    store: object
    publisher: object
    signer: VerdictSigner
    quarantine: object  # QuarantineManager
    hmac_key: str
    topic_verdicts: str = "guardian/verdicts"
    quarantine_threshold: int = 3
    count_unauthenticated: bool = False
    timeline: object | None = None
    replay: ReplayGuard = field(default_factory=ReplayGuard)
    plaus: PlausibilityChecker = field(default_factory=PlausibilityChecker)
    anomaly: EwmaAnomalyDetector = field(default_factory=EwmaAnomalyDetector)
    # Brain profile hook: (device_id, sensor_type) -> SensorLimits | None (limits from twin/meta)
    limits_provider: object | None = None
    # Brain profile: floor on the anomaly score (0.4 => the anomaly rule alone can only make a
    # reading 'suspicious', never 'rejected'); native default 0.0 keeps the original behaviour
    anomaly_min_score: float = 0.0
    _consecutive: dict = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------- steps (each independently unit-tested) ----------
    def step1_schema_signature(self, evt) -> StepResult | None:
        """Step 1: structural schema + HMAC signature. Returns a rejection or None."""
        ok, why = check_schema(evt)
        if not ok:
            return StepResult(1, "rejected", 0.0, why, self.count_unauthenticated)
        if not verify_event_signature(evt, self.hmac_key):
            return StepResult(1, "rejected", 0.0, "signature: invalid Twin event signature",
                              self.count_unauthenticated)
        return None

    def step2_replay(self, evt) -> StepResult | None:
        """Step 2: duplicate event_id / non-monotonic seq. Replays never count toward
        quarantine (an attacker replaying a healthy device must not get it quarantined)."""
        ok, why = self.replay.check_and_record(evt["event_id"], evt["device_id"], evt["seq"],
                                               time.monotonic())
        return None if ok else StepResult(2, "rejected", 0.0, why, False)

    def step3_plausibility(self, evt) -> StepResult | None:
        """Step 3: physical range / rate-of-change / unit / clock checks."""
        lim = self.limits_provider(evt["device_id"], evt["sensor_type"]) if self.limits_provider else None
        ok, why = self.plaus.check(evt["device_id"], evt["sensor_type"], evt["unit"],
                                   float(evt["value"]), parse_ts(evt["ts"]), utcnow(), limits=lim)
        return None if ok else StepResult(3, "rejected", 0.0, why)

    def step4_anomaly(self, evt) -> tuple[float, str]:
        """Step 4: EWMA z-score trust score in [0,1]."""
        return self.anomaly.score(evt["device_id"], float(evt["value"]), parse_ts(evt["ts"]))

    @staticmethod
    def step5_map(score: float) -> str:
        """Step 5: score -> verdict."""
        return map_score_to_verdict(score)

    # ---------- orchestration ----------
    async def process(self, raw) -> dict:
        """Run steps 1-6 on one raw event and return the SIGNED verdict message."""
        async with self._lock:  # per-instance ordering keeps replay/baseline state consistent
            return await self._process(raw)

    async def _process(self, raw) -> dict:
        evt = raw if isinstance(raw, dict) else None
        res = self.step1_schema_signature(raw)
        if res is None:
            res = self.step2_replay(evt)
        if res is None:
            res = self.step3_plausibility(evt)
        if res is None:
            score, why = self.step4_anomaly(evt)
            score = max(score, self.anomaly_min_score)
            res = StepResult(4, self.step5_map(score), score, why)

        device_id, event_id = self._identity(raw, res)
        await self.store.ensure_device(device_id)
        if res.step == 4 and await self.store.is_quarantined(device_id):
            res = StepResult(5, "rejected", 0.0,
                             f"device_quarantined (pipeline: {res.reason})", False)
        if res.verdict != "rejected" and evt is not None:
            ts = parse_ts(evt["ts"])
            self.plaus.accept(device_id, float(evt["value"]), ts)
            self.anomaly.update(device_id, float(evt["value"]), ts)
        return await self.step6_emit(evt, device_id, event_id, res)

    async def _unique_id(self, base: str, suffix: str) -> str:
        n = await self.store.count_verdict_ids(f"{base}#{suffix}-")
        return f"{base}#{suffix}-{n + 1}"

    def _identity(self, raw, res: StepResult) -> tuple[str, str]:
        claimed = raw.get("device_id") if isinstance(raw, dict) else None
        if not isinstance(claimed, str) or not claimed:
            dev = UNATTRIBUTED
        elif res.step == 1 and not self.count_unauthenticated:
            dev = UNATTRIBUTED  # unauthenticated claim: never touch the claimed device's state
        else:
            dev = claimed
        eid = raw.get("event_id") if isinstance(raw, dict) and isinstance(raw.get("event_id"), str) and raw.get("event_id") else None
        if eid is None:
            blob = raw if isinstance(raw, (bytes, bytearray)) else repr(raw).encode()
            eid = "invalid:" + hashlib.sha256(blob).hexdigest()[:16]
        return dev, eid

    async def step6_emit(self, evt, device_id: str, event_id: str, res: StepResult) -> dict:
        """Step 6: sign, persist (trust_verdicts + device score), publish, audit-log, contain."""
        stored_id = event_id
        if res.step == 2 or (res.step == 1 and await self.store.get_verdict(event_id)):
            stored_id = await self._unique_id(event_id, "replay" if res.step == 2 else "invalid")
        elif await self.store.get_verdict(event_id):
            stored_id = await self._unique_id(event_id, "dup")
        now = utcnow()
        verdict = self.signer.sign({
            "schema_version": VERDICT_SCHEMA_VERSION,
            "type": "trust_verdict",
            "event_id": event_id,
            "verdict_id": stored_id,
            "device_id": device_id,
            "claimed_device_id": evt.get("device_id") if isinstance(evt, dict) else None,
            "trust_score": round(float(res.score), 4),
            "verdict": res.verdict,
            "reason": res.reason,
            "failed_step": res.step if res.verdict == "rejected" else None,
            "issued_at": iso(now),
        })
        await self.store.insert_verdict(VerdictRow(stored_id, device_id, verdict["trust_score"],
                                                   res.verdict, res.reason, now))
        quarantined = await self.store.is_quarantined(device_id)
        if res.counts_for_quarantine or res.verdict != "rejected":
            # replays / unauthenticated rejects do not degrade the (possibly innocent) device
            dev = await self.store.get_device(device_id)
            prev_score = dev.trust_score if dev else 1.0
            new_score = round(0.7 * prev_score + 0.3 * verdict["trust_score"], 4)
            status = "quarantined" if quarantined else (
                "trusted" if res.verdict == "trusted" else "suspicious")
            await self.store.update_device(device_id, new_score, status)
        await self.store.append_audit(f"verdict_{res.verdict}", device_id,
                                      {"event_id": event_id, "verdict_id": stored_id,
                                       "trust_score": verdict["trust_score"], "reason": res.reason,
                                       "sig": verdict["sig"]["value"]})
        self.publisher.publish(f"{self.topic_verdicts}/{device_id}", verdict, qos=1)
        if self.timeline is not None and res.verdict != "trusted":
            self.timeline.add(f"verdict_{res.verdict}", device_id, res.reason,
                              "warning" if res.verdict == "suspicious" else "high", ref=event_id)
        await self._containment(device_id, event_id, res, quarantined)
        return verdict

    async def _containment(self, device_id: str, event_id: str, res: StepResult, quarantined: bool) -> None:
        if device_id == UNATTRIBUTED or quarantined:
            return
        if res.verdict == "rejected" and res.counts_for_quarantine:
            streak = self._consecutive.setdefault(device_id, [])
            streak.append(event_id)
            if len(streak) >= self.quarantine_threshold:
                evidence = list(streak)
                self._consecutive[device_id] = []
                await self.quarantine.quarantine(
                    device_id, f"{len(evidence)} consecutive rejected verdicts", evidence)
        elif res.verdict != "rejected":
            self._consecutive[device_id] = []
        # non-counting rejects (replay / unauthenticated) leave the streak untouched

    def reset_all(self) -> None:
        """Forget every baseline, streak and the replay cache (guardian/control op=reset)."""
        self._consecutive.clear()
        self.plaus._last.clear()
        self.anomaly._b.clear()
        self.replay._seen.clear()
        self.replay._last_seq.clear()

    def reset_device(self, device_id: str) -> None:
        """Forget baselines and streak for a device (called on manual release)."""
        self._consecutive.pop(device_id, None)
        self.plaus._last.pop(device_id, None)
        self.anomaly._b.pop(device_id, None)
