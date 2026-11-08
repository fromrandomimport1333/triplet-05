"""Run the math module (scoring + rule fallback): python -m src.scoring (from the brain/ folder).

One process: the rule fallback (src/rules/) is called by the scoring service, so a rule
proposal is always there when the decision is taken, even with the LLM offline.
"""

from __future__ import annotations

import signal
import threading

from ..common import config
from ..common.mqtt import BrainMqtt
from .service import ScoringService


def main() -> None:
    client: BrainMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = ScoringService(publish, seal_keys=config.outgoing_seal_keys())
    broker = config.load("broker")
    client = BrainMqtt(broker["client_ids"]["default_prefix"] + "scoring", logger=service.log)
    client.on_message = service.handle
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    service.log.info("scoring_started", budget_s=service.budget_s,
                     subscriptions=[t for t, _ in service.subscriptions()])
    tick = float(service.cfg["tick_s"])
    while not stop.wait(tick):
        service.tick()
    client.stop()
    service.log.info("scoring_stopped")


if __name__ == "__main__":
    main()
