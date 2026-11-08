"""Subscribes to Twin's event stream (twin/events/#) and feeds the detection pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from ..config import Settings
from .publisher import client_id, make_mqtt_client

log = logging.getLogger("guardian.mqtt")
Handler = Callable[[object], Awaitable[dict]]


class TwinSubscriber:
    """Bridges paho's network thread to the asyncio pipeline (QoS 1, persistent session)."""

    def __init__(self, settings: Settings, handler: Handler, loop: asyncio.AbstractEventLoop,
                 client=None) -> None:
        self.s, self.handler, self.loop = settings, handler, loop
        self.topic = f"{settings.topic_in}/#"
        self.client = client or make_mqtt_client(settings, client_id(settings, "sub"))
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect(self.s.mqtt_host, self.s.mqtt_port)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("connected to MQTT (%s), subscribing %s", reason_code, self.topic)
        client.subscribe(self.topic, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload: object = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = msg.payload  # still goes through the pipeline -> 'rejected' (schema)
        asyncio.run_coroutine_threadsafe(self.handler(payload), self.loop)


class BrainSubscriber:
    """Brain profile: subscribes the adapter's topics (QoS 1) and hands each message, with its
    retain flag, to BrainAdapter.handle on the asyncio loop."""

    def __init__(self, settings: Settings, adapter, loop: asyncio.AbstractEventLoop, client=None) -> None:
        self.s, self.adapter, self.loop = settings, adapter, loop
        self.client = client or make_mqtt_client(settings, client_id(settings, "sub"))
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect(self.s.mqtt_host, self.s.mqtt_port)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        topics = self.adapter.subscriptions()
        log.info("brain profile connected (%s), subscribing %s", reason_code, topics)
        client.subscribe([(t, 1) for t in topics])

    def _on_message(self, client, userdata, msg):
        asyncio.run_coroutine_threadsafe(
            self.adapter.handle(msg.topic, bytes(msg.payload), bool(msg.retain)), self.loop)
