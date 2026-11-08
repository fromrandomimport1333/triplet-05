"""MQTT client shared by every Brain module.

- MQTT 3.1.1 (TR-2), TLS against the triplet CA (SEC-1, SEC-2), optional client
  certificate for mTLS (SEC-6), username / password (SEC-3)
- automatic reconnect and re-subscribe (TR-8)
- optional Last Will and an "on connect" hook, used for brain/status (TR-6, BR-4)

Settings come from config/broker.yaml and config/security.yaml.

    client = BrainMqtt("brain-graph")
    client.subscribe("brain/internal/reading", qos=0)
    client.on_message = lambda topic, payload, retained: ...
    client.start()
"""

from __future__ import annotations

import ssl
import threading
from typing import Callable

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from . import config
from .log import EventLogger

MessageHandler = Callable[[str, bytes, bool], None]


class BrainMqtt:
    def __init__(
        self,
        client_id: str,
        *,
        will: tuple[str, str, int, bool] | None = None,
        logger: EventLogger | None = None,
    ):
        self.broker = config.load("broker")
        self.security = config.load("security")
        self.log = logger or EventLogger(client_id)
        self.on_message: MessageHandler | None = None
        self.on_connected: list[Callable[[], None]] = []
        self._subs: dict[str, int] = {}
        self._lock = threading.Lock()
        self.connected = threading.Event()

        c = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=paho.MQTTv311,
            clean_session=True,
        )
        self._configure_security(c)
        if will:
            topic, payload, qos, retain = will
            c.will_set(topic, payload, qos=qos, retain=retain)
        c.reconnect_delay_set(self.broker["reconnect_min_s"], self.broker["reconnect_max_s"])
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._client = c

    def _configure_security(self, c: paho.Client) -> None:
        tls = self.security.get("tls", {})
        if tls.get("enabled", True):
            c.tls_set(
                ca_certs=str(config.resolve(tls["ca_cert"])),
                certfile=str(config.resolve(tls["client_cert"])) if tls.get("client_cert") else None,
                keyfile=str(config.resolve(tls["client_key"])) if tls.get("client_key") else None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            c.tls_insecure_set(bool(tls.get("insecure_skip_hostname_check", False)))
        auth = self.security.get("auth", {})
        if auth.get("username"):
            c.username_pw_set(auth["username"], config.read_secret(auth.get("password_file")))

    # --- public API -----------------------------------------------------------------

    def subscribe(self, topic: str, qos: int = 1) -> None:
        """Subscribe now if connected, and again after every reconnect."""
        with self._lock:
            self._subs[topic] = qos
        if self.connected.is_set():
            self._client.subscribe(topic, qos)

    def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> paho.MQTTMessageInfo:
        return self._client.publish(topic, payload, qos=qos, retain=retain)

    def start(self) -> None:
        """Connect in the background; paho's loop thread reconnects on its own."""
        self._client.connect_async(
            self.broker["host"], int(self.broker["port"]), keepalive=int(self.broker["keepalive_s"])
        )
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    # --- paho callbacks ---------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.log.error("mqtt_connect_failed", reason=str(reason_code))
            return
        self.connected.set()
        with self._lock:
            subs = list(self._subs.items())
        for topic, qos in subs:
            client.subscribe(topic, qos)
        self.log.info("mqtt_connected", host=self.broker["host"], subscriptions=len(subs))
        for hook in self.on_connected:
            try:
                hook()
            except Exception as e:  # a hook must never stop the client
                self.log.error("on_connected_hook_failed", error=repr(e))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected.clear()
        self.log.warning("mqtt_disconnected", reason=str(reason_code))

    def _on_message(self, client, userdata, msg: paho.MQTTMessage) -> None:
        if self.on_message is None:
            return
        try:
            self.on_message(msg.topic, msg.payload, bool(msg.retain))
        except Exception as e:  # one bad message never stops the loop
            self.log.error("message_handler_failed", topic=msg.topic, error=repr(e))
