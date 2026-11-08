"""Run the timeline: python -m src.timeline (from the brain/ folder), then open http://127.0.0.1:8090

    python -m src.timeline [--host 0.0.0.0] [--port 8090]
"""

from __future__ import annotations

import argparse
import signal
import threading

from ..common import config
from ..common.mqtt import BrainMqtt
from .server import serve
from .service import TimelineService


def load_keys(security: dict) -> dict[str, bytes]:
    """HMAC keys to check signatures; a missing key only removes the lock mark (never exits)."""
    keys: dict[str, bytes] = {}
    for layer, path in security.get("hmac_keys", {}).items():
        text = config.read_secret(path)
        if text:
            keys[layer] = text.encode("utf-8")
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description="City Brain live timeline")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = config.load("timeline")
    keys = load_keys(config.load("security"))
    service = TimelineService(hmac_keys=keys)
    if not keys:
        service.log.warning("timeline_no_hmac_keys", detail="signatures are not checked")

    broker = config.load("broker")
    client = BrainMqtt(broker["client_ids"]["default_prefix"] + "timeline", logger=service.log)
    client.on_message = service.handle
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    srv, _ = serve(service, cfg, args.host, args.port)
    host, port = srv.server_address[:2]
    client.start()
    service.log.info("timeline_started", url=f"http://{host}:{port}/",
                     subscriptions=[t for t, _ in service.subscriptions()])
    print(f"City Brain timeline: http://{host}:{port}/", flush=True)
    while not stop.wait(1.0):
        pass
    srv.stop()
    client.stop()
    service.close()
    service.log.info("timeline_stopped")


if __name__ == "__main__":
    main()
