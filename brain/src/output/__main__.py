"""Run the output module: python -m src.output (from the brain/ folder)."""

from __future__ import annotations

import signal
import threading

from ..common import config
from ..common.mqtt import BrainMqtt
from .service import OutputService


def main() -> None:
    client: BrainMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = OutputService(publish, seal_keys=config.outgoing_seal_keys())
    broker = config.load("broker")
    client = BrainMqtt(broker["client_ids"]["default_prefix"] + "output", logger=service.log)
    client.on_message = service.handle
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    service.log.info("output_started", subscriptions=[t for t, _ in service.subscriptions()])
    tick = float(service.cfg.get("tick_s", 0.5))
    while not stop.wait(tick):
        service.tick()
    client.stop()
    service.log.info("output_stopped", **service.stats)


if __name__ == "__main__":
    main()
