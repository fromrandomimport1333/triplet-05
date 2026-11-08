"""Zero-infrastructure Guardian in the Brain protocol profile: real pipeline, quarantine
manager, audit log and BrainAdapter, with MemoryStore + MemoryBus and the synthetic Twin /
Brain stand-ins (mocks/brain_twin_sim.py). Simulated clock: one tick = one second."""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.adapter.envelope import open_envelope
from src.config import Settings
from src.core.events import VerdictSigner
from src.db.session import MemoryStore
from src.main import build_state
from src.mqtt.publisher import MemoryBus

from .brain_twin_sim import BrainSim, BrainTwinSim

LIMITS_FILE = str(Path(__file__).resolve().parents[1] / "config" / "sensor_limits.brain.json")


class BrainLocalStack:
    def __init__(self, **overrides) -> None:
        self.k_tg = secrets.token_hex(32)   # twin-guardian.key text
        self.k_gb = secrets.token_hex(32)   # guardian-brain.key text
        cfg = dict(mqtt_enabled=False, require_mtls=False, brain_protocol_enabled=True,
                   twin_guardian_key=self.k_tg, guardian_brain_key=self.k_gb, run_id="test1",
                   sensor_limits_file=LIMITS_FILE, anomaly_min_score=0.4,
                   topic_timeline="guardian/timeline", quarantine_threshold=3,
                   frozen_count=8, verdict_refresh_s=20.0)
        cfg.update(overrides)
        self.settings = Settings(**cfg)
        self.bus = MemoryBus()
        self.store = MemoryStore()
        self.loop = asyncio.new_event_loop()
        self.state = build_state(self.settings, store=self.store, publisher=self.bus,
                                 signer=VerdictSigner.generate())
        self.adapter = self.state.adapter
        # simulated clocks: start in the past so the native "future timestamp" check never trips
        self.start = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.twin = BrainTwinSim(self.k_tg, start=self.start)
        self.adapter.clock = self.twin.now
        self.adapter.mono = lambda: self.twin.t
        self.adapter.acks.started_at = 0.0
        self.brain = BrainSim(self.k_gb, self.twin.now)
        self.outcomes: list[str] = []

    # ------------------------------------------------------------------ driving
    def send(self, topic: str, payload, retained: bool = False) -> str:
        r = self.loop.run_until_complete(self.adapter.handle(topic, payload, retained))
        self.outcomes.append(r)
        return r

    def send_all(self, msgs) -> list[str]:
        return [self.send(t, p) for t, p in msgs]

    def tick(self, scenario: str = "calm", falsify=None, freeze=None) -> list[str]:
        out = self.send_all(self.twin.tick(scenario, falsify, freeze))
        self.loop.run_until_complete(self.adapter.tick())
        return out

    def advance(self, seconds: float) -> None:
        """Advance the clock without readings (ack timeouts)."""
        self.twin.t += seconds
        self.loop.run_until_complete(self.adapter.tick())

    def run(self, coro_fn, *args):
        return self.loop.run_until_complete(coro_fn(*args))

    # ------------------------------------------------------------------ observing
    def brain_out(self, prefix: str = "guardian/") -> list[dict]:
        """Every Brain-profile message we published: topic, verified body, retain flag, t."""
        out = []
        for topic, text, retain in self.bus.raw_history:
            if not topic.startswith(prefix):
                continue
            op = open_envelope(text, self.k_gb, "guardian")
            out.append({"topic": topic, "body": op.body, "retain": retain, "verified": op.ok,
                        "envelope": json.loads(text)})
        return out

    def verdicts(self, sensor_id: str | None = None) -> list[dict]:
        return [m for m in self.brain_out(self.settings.topic_out_verdict.split("{")[0])
                if sensor_id is None or m["body"]["sensor_id"] == sensor_id]

    def quarantines(self) -> list[dict]:
        return self.brain_out(self.settings.topic_out_quarantine.split("{")[0])

    def audit_actions(self) -> list[str]:
        return [r.action for r in self.run(self.store.audit_rows)]

    def close(self) -> None:
        self.loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
