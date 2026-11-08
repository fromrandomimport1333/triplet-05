"""Same interface as LocalStack, but against a RUNNING Guardian (docker compose) over real
MQTT (mTLS) + HTTPS JSON-RPC (mTLS + per-call JWT). Used by scenarios/ with --mode live.
All addresses/keys come from the environment (.env) — nothing hard-coded."""
from __future__ import annotations

import json
import os
import threading
import time

from src.config import load_settings
from src.mqtt.publisher import make_mqtt_client

from .brain_mock import BrainMock, HttpRpc
from .twin_mock import TwinMock


class LiveStack:  # pragma: no cover - needs the docker stack
    def __init__(self) -> None:
        s = self.settings = load_settings()
        self.keys = s.jwt_keys
        self.base_url = os.environ["GUARDIAN_RPC_URL"]
        self.cert_dir = os.environ.get("SCENARIO_CERT_DIR", "certs")
        self.verdicts: list[dict] = []
        self._by_id: dict[str, list[dict]] = {}
        self._cv = threading.Condition()
        self.client = make_mqtt_client(s, "scenario-runner")
        self.client.on_message = self._on_msg
        self.client.connect(s.mqtt_host, s.mqtt_port)
        self.client.subscribe([(f"{s.topic_verdicts}/#", 1), (f"{s.topic_control}/#", 1)])
        self.client.loop_start()
        pub_pem = self.rpc_raw_pubkey()
        self.run_id = format(int(time.time()), "x")
        self.twin = TwinMock(s.twin_event_hmac_key, self._publish, s.topic_in, guardian_pubkey_pem=pub_pem,
                             seq_base=int(time.time() * 1000))
        self.brain = BrainMock(self.rpc("brain"), self.twin)
        time.sleep(0.5)

    def rpc_raw_pubkey(self) -> str:
        import ssl

        import httpx
        c = self.cert_dir
        ctx = ssl.create_default_context(cafile=f"{c}/ca.pem")
        ctx.load_cert_chain(f"{c}/brain.pem", f"{c}/brain.key")
        with httpx.Client(verify=ctx, timeout=5) as h:
            return h.get(f"{self.base_url}/.well-known/guardian-verdict-key").text

    def _publish(self, topic: str, payload: dict) -> None:
        # no wait_for_publish: this is also called from paho's network thread (Twin acks)
        self.client.publish(topic, json.dumps(payload), qos=1)

    def dev(self, name: str) -> str:
        """Unique device id per run so re-runs against a persistent DB stay independent."""
        return f"{name}-{self.run_id}"

    def _on_msg(self, client, userdata, msg) -> None:
        p = json.loads(msg.payload)
        if msg.topic.startswith(self.settings.topic_control + "/"):
            self.twin.on_control(msg.topic, p)
            return
        if p.get("type") != "trust_verdict":
            return
        with self._cv:
            self.verdicts.append(p)
            self._by_id.setdefault(p["event_id"], []).append(p)
            self._cv.notify_all()

    def wait_verdict(self, event_id: str, nth: int = 1, timeout: float = 10.0) -> dict:
        end = time.monotonic() + timeout
        with self._cv:
            while len(self._by_id.get(event_id, [])) < nth:
                if not self._cv.wait(max(0.0, end - time.monotonic())):
                    raise TimeoutError(f"no verdict #{nth} for {event_id} within {timeout}s")
            return self._by_id[event_id][nth - 1]

    def rpc(self, role: str) -> HttpRpc:
        c = self.cert_dir
        return HttpRpc(self.base_url, self.keys[role], role, ca=f"{c}/ca.pem",
                       cert=f"{c}/{role}.pem", key=f"{c}/{role}.key")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
