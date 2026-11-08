"""Run the case memory: python -m src.memory (from the brain/ folder)."""

from __future__ import annotations

import signal
import threading
import time

from ..common import config
from ..common.mqtt import BrainMqtt
from .service import MemoryService


def main() -> None:
    lock = threading.Lock()          # paho thread (messages) and main thread (lookups) share the state
    client: BrainMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = MemoryService(publish, clock=time.time, seal_keys=config.outgoing_seal_keys())
    client = BrainMqtt(config.load("broker")["client_ids"]["default_prefix"] + "memory", logger=service.log)

    def on_message(topic: str, payload: bytes, retained: bool) -> None:
        with lock:
            service.handle(topic, payload, retained)

    client.on_message = on_message
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    service.log.info("memory_started", library_cases=len(service.library),
                     library=service.cfg["library"]["name"])
    while not stop.wait(1.0):
        with lock:
            service.tick()
    client.stop()
    service.log.info("memory_stopped")


if __name__ == "__main__":
    main()
