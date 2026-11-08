"""Run the Twin mock: python -m mocks.twin (from the brain/ folder)."""

from __future__ import annotations

import signal
import sys
import threading
import time

from src.common import config
from src.common.mqtt import BrainMqtt

from .service import TwinService


class TwinMqtt(BrainMqtt):
    """Same broker and TLS settings as the Brain, but the broker account of the Twin."""

    def _configure_security(self, c) -> None:
        super()._configure_security(c)
        auth = config.load("twin-mock").get("auth", {})
        if auth.get("username"):
            c.username_pw_set(auth["username"], config.read_secret(auth.get("password_file")))


def main() -> None:
    cfg = config.load("twin-mock")
    text = config.read_secret(config.load("security")["hmac_keys"]["twin"])
    if not text:
        sys.exit("missing HMAC key twin-brain (see brain/README.md)")

    client: TwinMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = TwinService(publish, key=text.encode("utf-8"))
    status_topic = service.mapper.status("offline")[0]
    client = TwinMqtt(cfg["client_id"], will=(status_topic, service.status_payload("offline"), 1, True), logger=service.log)
    client.on_message = service.handle
    client.on_connected.append(service.on_connect)
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    sim = cfg["sim"]
    if sim["mode"] == "auto":
        tick_s = float(sim["tick_s"])
        next_at = time.monotonic() + tick_s
        while not stop.wait(max(0.0, next_at - time.monotonic())):
            service.tick()
            next_at += tick_s
    else:
        stop.wait()

    # clean stop: say offline ourselves (the Last Will only fires on a lost connection)
    info = client.publish(status_topic, service.status_payload("offline"), 1, True)
    if client.connected.is_set():
        info.wait_for_publish(timeout=2)
    client.stop()


if __name__ == "__main__":
    main()
