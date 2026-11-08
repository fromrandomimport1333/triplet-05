"""Publishes Guardian output: guardian/verdicts/<device_id>, guardian/control/<device_id>
(quarantine/release, retained) and the shared timeline topic. publish_raw() sends an exact
text payload (Brain-profile envelopes must reach the wire byte for byte)."""
from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Callable, Protocol

from ..config import Settings


class Publisher(Protocol):
    def publish(self, topic: str, payload: dict, *, retain: bool = False, qos: int = 1) -> None: ...

    def publish_raw(self, topic: str, text: str, *, retain: bool = False, qos: int = 1) -> None: ...


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard match (+ single level, # multi level)."""
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t) or (seg != "+" and seg != t[i]):
            return False
    return len(p) == len(t)


class MemoryBus:
    """In-process MQTT stand-in used by tests, mocks and local scenarios (supports retain)."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Callable[[str, dict], None]]] = []
        self.retained: dict[str, dict] = {}
        self.history: list[tuple[str, dict]] = []
        self.raw_history: list[tuple[str, str, bool]] = []   # (topic, exact text, retain)
        self._lock = threading.RLock()

    def publish(self, topic: str, payload: dict, *, retain: bool = False, qos: int = 1) -> None:
        payload = json.loads(json.dumps(payload))  # wire round-trip: no shared references
        with self._lock:
            self.history.append((topic, payload))
            if retain:
                self.retained[topic] = payload
            subs = list(self._subs)
        for pattern, cb in subs:
            if topic_matches(pattern, topic):
                cb(topic, payload)

    def publish_raw(self, topic: str, text: str, *, retain: bool = False, qos: int = 1) -> None:
        with self._lock:
            self.raw_history.append((topic, text, retain))
        self.publish(topic, json.loads(text), retain=retain, qos=qos)

    def subscribe(self, pattern: str, cb: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._subs.append((pattern, cb))
            retained = [(t, p) for t, p in self.retained.items() if topic_matches(pattern, t)]
        for t, p in retained:
            cb(t, p)


class MqttPublisher:
    """paho-mqtt publisher over TLS (mTLS in the native profile, account+password in the Brain
    profile). With autostart=False the caller can set a Last Will before start()."""

    def __init__(self, settings: Settings, client=None, *, autostart: bool = True) -> None:
        self.s = settings
        self.client = client or make_mqtt_client(settings, client_id(settings, "pub"))
        self.on_connected = None  # callable() run after every (re)connection, e.g. status online
        self.client.on_connect = self._on_connect
        if autostart:
            self.start()

    def set_will(self, topic: str, text: str) -> None:
        """Last Will (retained, QoS 1): must be set before start()."""
        self.client.will_set(topic, text, qos=1, retain=True)

    def start(self) -> None:
        self.client.connect(self.s.mqtt_host, self.s.mqtt_port)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if self.on_connected is not None and not getattr(reason_code, "is_failure", False):
            self.on_connected()

    def publish(self, topic: str, payload: dict, *, retain: bool = False, qos: int = 1) -> None:
        self.client.publish(topic, json.dumps(payload), qos=qos, retain=retain)

    def publish_raw(self, topic: str, text: str, *, retain: bool = False, qos: int = 1) -> None:
        self.client.publish(topic, text, qos=qos, retain=retain)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


_RUN_SUFFIX = secrets.token_hex(3)


def client_id(settings: Settings, role: str) -> str:
    """Unique, configurable MQTT client id per client (two clients with one id kick each other
    off). MQTT_CLIENT_ID_PUB / MQTT_CLIENT_ID_SUB override; default <MQTT_CLIENT_ID>-<role>-<rnd>."""
    explicit = {"pub": settings.mqtt_client_id_pub, "sub": settings.mqtt_client_id_sub}.get(role, "")
    return explicit or f"{settings.mqtt_client_id}-{role}-{_RUN_SUFFIX}"


def make_mqtt_client(settings: Settings, client_id: str):
    """Build a paho client from settings. TLS with the CA; a client certificate only if
    MQTT_CERT_FILE is set (native mTLS); account + password file if MQTT_USERNAME is set."""
    import ssl

    import paho.mqtt.client as mqtt  # imported lazily: not needed for unit tests

    if not settings.mqtt_host:
        raise RuntimeError("MQTT_HOST is not set (see .env.example)")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                    protocol=mqtt.MQTTv311)
    if settings.mqtt_tls:
        c.tls_set(ca_certs=settings.mqtt_ca or None, certfile=settings.mqtt_cert or None,
                  keyfile=settings.mqtt_key or None, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    if settings.mqtt_username:
        pw = Path(settings.mqtt_password_file).read_text(encoding="utf-8").strip() \
            if settings.mqtt_password_file else ""
        c.username_pw_set(settings.mqtt_username, pw)
    return c
