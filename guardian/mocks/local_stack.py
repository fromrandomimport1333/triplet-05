"""Zero-infrastructure Guardian stack: real FastAPI app + real pipeline, with MemoryStore,
in-process MemoryBus (MQTT stand-in), TwinMock and BrainMock. Used by tests and by the
root scenarios/ in --mode local (default). mTLS is exercised separately (unit tests + docker)."""
from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from src.config import Settings
from src.core.events import VerdictSigner
from src.db.session import MemoryStore
from src.main import build_state, create_app
from src.mqtt.publisher import MemoryBus

from .brain_mock import BrainMock, InProcessRpc
from .twin_mock import TwinMock


class LocalStack:
    def __init__(self, *, stuck_window_s: int = 300, quarantine_threshold: int = 3,
                 count_unauthenticated: bool = False) -> None:
        self.keys = {r: secrets.token_hex(32) for r in ("brain", "twin", "ui", "admin")}
        self.hmac_key = secrets.token_hex(32)
        self.settings = Settings(mqtt_enabled=False, require_mtls=False, jwt_keys=self.keys,
                                 twin_event_hmac_key=self.hmac_key, stuck_window_s=stuck_window_s,
                                 quarantine_threshold=quarantine_threshold,
                                 quarantine_count_unauthenticated=count_unauthenticated)
        self.bus = MemoryBus()
        self.store = MemoryStore()
        self.signer = VerdictSigner.generate()
        self.state = build_state(self.settings, store=self.store, publisher=self.bus, signer=self.signer)
        self.app = create_app(self.settings, state=self.state)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.twin = TwinMock(self.hmac_key, self.bus.publish, self.settings.topic_in,
                             guardian_pubkey_pem=self.signer.public_key_pem())
        self.bus.subscribe(f"{self.settings.topic_control}/#", self.twin.on_control)
        self.verdicts: list[dict] = []
        self.bus.subscribe(f"{self.settings.topic_in}/#", self._to_guardian)
        self.brain = BrainMock(self.rpc("brain"), self.twin)

    def _to_guardian(self, topic: str, payload: dict) -> None:
        self.verdicts.append(self.client.portal.call(self.state.pipeline.process, payload))

    def process_raw(self, raw) -> dict:
        """Feed an arbitrary (possibly malformed) payload straight into the pipeline."""
        v = self.client.portal.call(self.state.pipeline.process, raw)
        self.verdicts.append(v)
        return v

    def wait_verdict(self, event_id: str, nth: int = 1, timeout: float = 10.0) -> dict:
        """Local mode is synchronous: the verdict already exists once emit() returns."""
        got = [v for v in self.verdicts if v["event_id"] == event_id]
        if len(got) < nth:
            raise TimeoutError(f"no verdict #{nth} for {event_id}")
        return got[nth - 1]

    def dev(self, name: str) -> str:
        return name

    def rpc(self, role: str) -> InProcessRpc:
        return InProcessRpc(self.client, self.keys[role], role)

    def run(self, coro_fn, *args):
        return self.client.portal.call(coro_fn, *args)

    def published(self, prefix: str) -> list[tuple[str, dict]]:
        return [(t, p) for t, p in self.bus.history if t.startswith(prefix)]

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
