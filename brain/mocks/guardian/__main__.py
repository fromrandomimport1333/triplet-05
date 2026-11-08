"""Run the Guardian mock: python -m mocks.guardian (from the brain/ folder)."""

from __future__ import annotations

import signal
import sys
import threading

from src.common import config
from src.common.mqtt import BrainMqtt

from .service import GuardianService


class GuardianMqtt(BrainMqtt):
    """Same broker and TLS settings as the Brain, but the broker account of the Guardian."""

    def _configure_security(self, c) -> None:
        super()._configure_security(c)
        auth = config.load("guardian-mock").get("auth", {})
        if auth.get("username"):
            c.username_pw_set(auth["username"], config.read_secret(auth.get("password_file")))


def main() -> None:
    cfg = config.load("guardian-mock")
    keys = config.load("security")["hmac_keys"]
    text = config.read_secret(keys["guardian"])
    if not text:
        sys.exit("missing HMAC key guardian-brain (see brain/README.md)")
    twin_text = config.read_secret(keys["twin"])   # optional: check the readings' HMAC

    client: GuardianMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = GuardianService(publish, key=text.encode("utf-8"),
                              twin_key=twin_text.encode("utf-8") if twin_text else None)
    status_topic = service.status_topic()
    client = GuardianMqtt(cfg["client_id"], will=(status_topic, service.status_payload("offline"), 1, True),
                          logger=service.log)
    client.on_message = service.handle
    client.on_connected.append(service.on_connect)
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    while not stop.wait(1.0):
        service.tick()

    # clean stop: say offline ourselves (the Last Will only fires on a lost connection)
    info = client.publish(status_topic, service.status_payload("offline"), 1, True)
    if client.connected.is_set():
        info.wait_for_publish(timeout=2)
    client.stop()


if __name__ == "__main__":
    main()
