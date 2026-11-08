"""Run the graph module: python -m src.graph (from the brain/ folder)."""

from __future__ import annotations

import signal
import threading

from ..common import config
from ..common.mqtt import BrainMqtt
from .http import make_server
from .service import GraphService


def main() -> None:
    cfg = config.load("graph")
    service = GraphService()
    client = BrainMqtt(config.load("broker")["client_ids"]["default_prefix"] + "graph", logger=service.log)
    client.on_message = service.handle
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    server = make_server(service, cfg["http"]["host"], int(cfg["http"]["port"]))
    threading.Thread(target=server.serve_forever, name="graph-http", daemon=True).start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    service.log.info("graph_started", http=f"{cfg['http']['host']}:{cfg['http']['port']}",
                     subscriptions=[t for t, _ in service.subscriptions()])
    while not stop.wait(1.0):        # a timeout keeps Ctrl+C working on Windows
        pass
    server.shutdown()
    client.stop()
    service.log.info("graph_stopped")


if __name__ == "__main__":
    main()
