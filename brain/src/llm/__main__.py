"""Run the LLM module: python -m src.llm (from the brain/ folder).

The mock LLM (mocks/mock_llm) runs this same service with a scripted backend.
"""

from __future__ import annotations

import queue
import signal
import sys
import threading
from typing import Any

from ..common import config
from ..common.mqtt import BrainMqtt
from .backends import LlmBackend, OllamaBackend
from .context import GraphContextClient
from .service import LlmService


def build_backend(cfg: dict[str, Any]) -> LlmBackend:
    kind = cfg["backend"]["kind"]
    if kind == "ollama":
        return OllamaBackend(cfg["backend"])
    sys.exit(f"unknown llm backend kind {kind!r} (the mock is started with python -m mocks.mock_llm)")


def run(backend: LlmBackend, client_suffix: str = "llm") -> None:
    cfg = config.load("llm")
    topics = config.load("topics")
    graph_url = cfg["context"].get("graph_url") or ""
    service = LlmService(
        lambda *a: client.publish(*a),
        backend,
        graph=GraphContextClient(graph_url) if graph_url else None,
        seal_keys=config.outgoing_seal_keys(),
    )
    status = topics["internal"]["llm_status"]
    broker = config.load("broker")
    client = BrainMqtt(
        broker["client_ids"]["default_prefix"] + client_suffix,
        will=(status["topic"], service.status_payload("offline", "connection lost"), int(status["qos"]), True),
        logger=service.log,
    )
    client.on_message = service.handle
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    if cfg["backend"].get("warmup", True):
        try:
            backend.warmup(timeout_s=120)
            service.log.info("llm_warmed_up", model=backend.name)
        except Exception as e:  # the health check reports it; never stop the module for this
            service.log.warning("llm_warmup_failed", error=repr(e)[:200])

    def worker() -> None:
        while not stop.is_set():
            try:
                job = service.next_job(timeout_s=0.5)
            except queue.Empty:
                continue
            try:
                service.process(job)
            except Exception as e:  # one bad incident never stops the worker
                service.log.error("llm_process_failed", incident_id=job.incident.incident_id, error=repr(e))

    threading.Thread(target=worker, name="llm-worker", daemon=True).start()
    client.start()
    service.log.info("llm_started", model=backend.name, subscriptions=[t for t, _ in service.subscriptions()])
    while not stop.wait(1.0):
        service.tick()

    # clean stop: say offline ourselves (the Last Will only fires on a lost connection)
    info = client.publish(status["topic"], service.status_payload("offline", "stopped"), int(status["qos"]), True)
    if client.connected.is_set():
        info.wait_for_publish(timeout=2)
    client.stop()
    service.log.info("llm_stopped")


def main() -> None:
    run(build_backend(config.load("llm")))


if __name__ == "__main__":
    main()
