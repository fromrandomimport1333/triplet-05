"""Run the input module: python -m src.input (from the brain/ folder)."""

from __future__ import annotations

import signal
import sys
import threading

from ..common import config
from ..common.envelope import load_ed25519_public_key
from ..common.mqtt import BrainMqtt
from .service import InputService


def load_keys(security: dict) -> tuple[dict[str, bytes], object]:
    keys: dict[str, bytes] = {}
    for layer, path in security["hmac_keys"].items():
        text = config.read_secret(path)
        if not text:
            sys.exit(f"missing HMAC key for {layer}: {config.resolve(path)} (see brain/README.md)")
        keys[layer] = text.encode("utf-8")
    ed_key = None
    sig = security.get("guardian_signature", {})
    if sig.get("mode") == "ed25519":
        text = config.read_secret(sig.get("ed25519_public_key"))
        if not text:
            sys.exit("guardian_signature.mode is ed25519 but the public key file is missing")
        ed_key = load_ed25519_public_key(text)
    return keys, ed_key


def main() -> None:
    security = config.load("security")
    topics = config.load("topics")
    keys, ed_key = load_keys(security)

    client: BrainMqtt | None = None

    def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
        client.publish(topic, payload, qos, retain)

    service = InputService(publish, hmac_keys=keys, ed25519_key=ed_key)
    status = topics["external_out"]["status"]
    client = BrainMqtt(
        config.load("broker")["client_ids"]["input"],
        will=(status["topic"], service.status_payload("offline"), int(status["qos"]), True),
        logger=service.log,
    )
    client.on_message = service.handle
    client.on_connected.append(
        lambda: client.publish(status["topic"], service.status_payload("online"), int(status["qos"]), True)
    )
    for topic, qos in service.subscriptions():
        client.subscribe(topic, qos)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    client.start()
    service.log.info("input_started", subscriptions=[t for t, _ in service.subscriptions()])
    tick = float(service.cfg["layers"]["tick_s"])
    while not stop.wait(tick):
        service.tick()

    # clean stop: say offline ourselves (the Last Will only fires on a lost connection)
    info = client.publish(status["topic"], service.status_payload("offline"), int(status["qos"]), True)
    if client.connected.is_set():
        info.wait_for_publish(timeout=2)
    client.stop()
    service.log.info("input_stopped")


if __name__ == "__main__":
    main()
