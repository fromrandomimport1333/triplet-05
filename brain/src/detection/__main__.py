"""Run the detection module: python -m src.detection (from the brain/ folder)."""

from __future__ import annotations

import signal
import threading
import time

from ..common import config
from ..common.mqtt import BrainMqtt
from .service import DetectionService


def main() -> None:
    cfg = config.load("detection")
    lock = threading.Lock()          # paho thread (messages) and main thread (tick) share the state
    client: BrainMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = DetectionService(publish, clock=time.time, seal_keys=config.outgoing_seal_keys())
    client = BrainMqtt(config.load("broker")["client_ids"]["default_prefix"] + "detection", logger=service.log)

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
    service.log.info("detection_started", subscriptions=[t for t, _ in service.subscriptions()])
    while not stop.wait(float(cfg["lifecycle"]["tick_s"])):
        with lock:
            service.tick()
    client.stop()
    service.log.info("detection_stopped")


if __name__ == "__main__":
    main()
