"""FastAPI entrypoint: wires config -> store -> signer -> pipeline -> MQTT -> JSON-RPC API."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from .api.routes import build_router
from .api.tools import GuardianTools
from .config import Settings, load_settings
from .containment.quarantine import QuarantineManager
from .core.events import VerdictSigner
from .core.security import JwtVerifier
from .core.timeline import Timeline
from .db.session import make_store
from .detection import DetectionPipeline
from .detection.anomaly import EwmaAnomalyDetector
from .detection.replay_guard import ReplayGuard

log = logging.getLogger("guardian")


@dataclass
class AppState:
    settings: Settings
    store: object
    publisher: object
    signer: VerdictSigner
    timeline: Timeline
    quarantine: QuarantineManager
    pipeline: DetectionPipeline
    tools: GuardianTools
    jwt: JwtVerifier
    subscriber: object | None = None
    adapter: object | None = None      # BrainAdapter when BRAIN_PROTOCOL_ENABLED


def build_state(settings: Settings, *, store=None, publisher=None, signer=None) -> AppState:
    """Assemble all components. Tests/scenarios inject MemoryStore + MemoryBus."""
    store = store or make_store(settings.database_url)
    live_pub = None
    if publisher is None:
        from .mqtt.publisher import MqttPublisher
        # Brain profile: the Last Will must be set before connecting, so start after wiring
        publisher = live_pub = MqttPublisher(settings, autostart=not settings.brain_protocol_enabled)
    if settings.brain_protocol_enabled and not settings.twin_event_hmac_key:
        # internal key: verified kit readings are re-signed as native events before the pipeline
        import secrets
        from dataclasses import replace
        settings = replace(settings, twin_event_hmac_key=secrets.token_hex(32))
    signer = signer or VerdictSigner.from_file_or_generate(settings.verdict_signing_key_path)
    timeline = Timeline(publisher, settings.topic_timeline)
    qm = QuarantineManager(store, publisher, signer, topic_control=settings.topic_control,
                           topic_verdicts=settings.topic_verdicts,
                           rollback_window_s=settings.rollback_window_s, timeline=timeline)
    pipeline = DetectionPipeline(
        store=store, publisher=publisher, signer=signer, quarantine=qm,
        hmac_key=settings.twin_event_hmac_key, topic_verdicts=settings.topic_verdicts,
        quarantine_threshold=settings.quarantine_threshold,
        count_unauthenticated=settings.quarantine_count_unauthenticated, timeline=timeline,
        replay=ReplayGuard(settings.replay_cache_size, settings.replay_window_s),
        anomaly=EwmaAnomalyDetector(stuck_window_s=settings.stuck_window_s))
    tools = GuardianTools(store, signer, qm, pipeline)
    jwtv = JwtVerifier(settings.jwt_keys, settings.jwt_audience, settings.jwt_max_ttl_s)
    st = AppState(settings, store, publisher, signer, timeline, qm, pipeline, tools, jwtv)
    if settings.brain_protocol_enabled:
        from .adapter.brain import BrainAdapter
        st.adapter = BrainAdapter(settings, st, publisher)
        if live_pub is not None:
            live_pub.set_will(settings.topic_out_status, st.adapter.status_payload("offline"))
            live_pub.on_connected = lambda: st.adapter.announce("online")
            live_pub.start()
    return st


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    settings = settings or (state.settings if state else load_settings())
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = holder["state"]
        await st.store.connect()
        await st.store.append_audit("guardian_started", None, {"verdict_key_kid": st.signer.kid})
        ticker = None
        if settings.mqtt_enabled and st.subscriber is None and settings.mqtt_host:
            if st.adapter is not None:
                from .mqtt.subscriber import BrainSubscriber
                st.subscriber = BrainSubscriber(settings, st.adapter, asyncio.get_running_loop())
            else:
                from .mqtt.subscriber import TwinSubscriber
                st.subscriber = TwinSubscriber(settings, st.pipeline.process, asyncio.get_running_loop())
            st.subscriber.start()
        if st.adapter is not None:
            async def _tick():
                while True:
                    await asyncio.sleep(1.0)
                    await st.adapter.tick()
            ticker = asyncio.create_task(_tick())
        yield
        if ticker is not None:
            ticker.cancel()
        if st.subscriber is not None:
            st.subscriber.stop()
        await st.store.close()

    st = state or build_state(settings)
    holder["state"] = st
    app = FastAPI(title="Guardian", version="1.2.0", lifespan=lifespan)
    app.state.guardian = st
    app.include_router(build_router(st))
    return app


def run() -> None:
    import uvicorn

    from .core.security import make_mtls_h11_protocol

    logging.basicConfig(level=logging.INFO)
    s = load_settings()
    kw = {}
    if s.require_mtls:
        import ssl
        kw = dict(ssl_certfile=s.tls_cert, ssl_keyfile=s.tls_key, ssl_ca_certs=s.tls_ca,
                  ssl_cert_reqs=ssl.CERT_REQUIRED, http=make_mtls_h11_protocol())
    uvicorn.run(create_app(s), host=s.api_host, port=s.api_port, **kw)


if __name__ == "__main__":
    run()
