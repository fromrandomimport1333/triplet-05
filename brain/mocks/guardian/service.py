"""Guardian mock service: the verdict core on MQTT.

Independent of MQTT like the Twin mock: `handle()` takes (topic, payload, retained) and everything
goes out through `publish(topic, payload, qos, retain)`, so it is testable without a broker.

In:   city/{zone}/sensor/{type}/{id}  readings from the Twin (sealed for the Brain with the twin-brain key:
                                      the mock reads the body, and checks the HMAC when it has that key)
      twin/meta                       city description (else the city of mocks/city.yaml)
      guardian/control                test control (mode / reset), sealed with the guardian-brain key
Out:  guardian/verdict/{sensor_id}, guardian/quarantine/{device_id}, guardian/status (all retained),
      sealed with the guardian-brain key. Topic names from topics.yaml (external_in, source guardian).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from src.common import config
from src.common.envelope import (
    EnvelopeError,
    parse_envelope,
    seal,
    seal_multi,
    verify_hmac,
)
from src.common.ids import IdGenerator
from src.common.log import EventLogger, iso_utc

from ..twin.city import load_city
from .core import GuardianCore

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain


def _subscription(pattern: str) -> str:
    return "/".join("+" if p.startswith("{") else p for p in pattern.split("/"))


class GuardianService:
    def __init__(
        self,
        publish: Publisher,
        *,
        key: bytes,
        twin_key: bytes | None = None,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        guardian_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
    ):
        self.cfg = guardian_cfg or config.load("guardian-mock")
        self.topics_cfg = topics_cfg or config.load("topics")
        self._publish = publish
        self._key = key
        self._twin_key = twin_key if self.cfg.get("verify_twin_hmac", True) else None
        self.clock = clock
        self.log = logger or EventLogger("guardian-mock")
        self._lock = threading.RLock()
        routes = self.topics_cfg["external_in"]
        self.routes = {k: v for k, v in routes.items() if v["source"] == "guardian"}
        self.reading_route = routes["reading"]
        self.meta_topic = routes["meta"]["pattern"]
        self.control_topic = self.cfg["topics"]["control"]
        city = load_city(config.resolve(self.cfg["city"])) if self.cfg.get("city") else load_city()
        self.core = GuardianCore(self.cfg, city.sensors, set(city.actuators) | set(city.units))
        self._resets = 0
        self.ids = IdGenerator(self._run_id())
        self.ignored = 0
        # modes of the config (e.g. quarantine:valve-01): their notices go out on the first connect
        self._pending = self.core.apply_modes(list(self.cfg.get("modes") or []), clock())

    # --- helpers --------------------------------------------------------------------

    def _run_id(self) -> str:
        base = f"gd{int(self.clock())}"
        return base if self._resets == 0 else f"{base}r{self._resets}"

    def _now_iso(self) -> str:
        return iso_utc(self.clock())

    def _seal(self, body: dict[str, Any]) -> str:
        if self.cfg.get("envelope", "hmac") == "hmacs":
            return seal_multi(body, {"brain": self._key})
        return seal(body, self._key)

    def _route(self, kind: str, **fields: str) -> tuple[str, int]:
        r = self.routes[kind]
        return r["pattern"].format(**fields), int(r["qos"])

    def message(self, out: dict[str, Any]) -> tuple[str, dict[str, Any], int, bool]:
        """Core output -> (topic, body, qos, retain). Bodies follow VerdictBody / QuarantineBody / StatusBody."""
        body: dict[str, Any] = {"id": self.ids.next(), "timestamp": self._now_iso(), "source": "guardian"}
        if out["kind"] == "verdict":
            topic, qos = self._route("verdict", sensor_id=out["sensor_id"])
            if out.get("correlation_id"):
                body["correlation_id"] = out["correlation_id"]
            body.update({k: out[k] for k in ("sensor_id", "verdict", "reason", "trust_score", "details") if k in out})
        elif out["kind"] == "quarantine":
            topic, qos = self._route("quarantine", device_id=out["device_id"])
            body.update(device_id=out["device_id"], state=out["state"], reason=out["reason"])
        else:
            topic, qos = self._route("guardian_status")
            body["status"] = out["status"]
        return topic, body, qos, True

    def _send(self, outputs: list[dict[str, Any]]) -> None:
        for out in outputs:
            topic, body, qos, retain = self.message(out)
            self._publish(topic, self._seal(body), qos, retain)
            if out["kind"] != "verdict" or out.get("verdict") != "trusted":
                self.log.info(f"guardian_{out['kind']}", topic=topic, **{k: v for k, v in body.items() if k not in ("id", "timestamp", "source")})

    def status_payload(self, status: str) -> str:
        """guardian/status body, sealed (used for the Last Will)."""
        return self._seal(self.message({"kind": "status", "status": status})[1])

    def status_topic(self) -> str:
        return self._route("guardian_status")[0]

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(_subscription(self.reading_route["pattern"]), int(self.reading_route.get("qos", 0))),
                (self.meta_topic, 1), (self.control_topic, 1)]

    # --- publishing -----------------------------------------------------------------

    def on_connect(self) -> None:
        """After every (re)connect: online (unless in offline mode), then the current verdicts."""
        with self._lock:
            if not self.core.offline:
                self._send(self.core.go_online(self.clock()) + self._pending)
                self._pending = []
            self.log.info("guardian_online", offline_mode=self.core.offline)

    def tick(self) -> None:
        """Every second: refresh verdicts older than refresh_s, release slow verdicts."""
        with self._lock:
            self._send(self.core.tick(self.clock()))

    # --- inputs ---------------------------------------------------------------------

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        """Process one incoming message. Never raises."""
        try:
            with self._lock:
                if topic == self.control_topic:
                    if not retained:
                        self._control(self._open(payload, self._key, "guardian"))
                elif topic == self.meta_topic:
                    meta = self._open(payload, self._twin_key, "guardian")
                    self.core.set_city(*GuardianCore.sensors_from_meta(meta))
                elif not retained:
                    body = self._open(payload, self._twin_key, "guardian")
                    self._send(self.core.observe(body, self.clock()))
        except (EnvelopeError, ValueError, KeyError, TypeError) as e:
            self.ignored += 1
            self.log.warning("message_ignored", topic=topic, reason=str(getattr(e, "detail", e))[:200])
        except Exception as e:  # a bug must not stop the loop
            self.log.error("guardian_internal_error", topic=topic, error=repr(e))

    @staticmethod
    def _open(payload: bytes | str, key: bytes | None, self_name: str) -> dict[str, Any]:
        env = parse_envelope(payload)
        if key is not None:
            verify_hmac(env, key, self_name)
        body = json.loads(env.body)
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
        return body

    def _control(self, body: dict[str, Any]) -> None:
        op = body["op"]
        now = self.clock()
        if op == "mode":
            modes = body.get("modes", [body["mode"]] if "mode" in body else None)
            if not isinstance(modes, list) or not modes:
                raise ValueError("mode needs `mode` or a non-empty `modes` list")
            self._send(self.core.apply_modes(modes, now))
        elif op == "reset":
            # back to the config (or to `modes`): quarantines released, history forgotten, online again
            released = self.core.release_all()
            was_offline = self.core.offline
            self.core.reset()
            self._resets += 1
            self.ids = IdGenerator(self._run_id())
            self._send(released + [{"kind": "status", "status": "online"}])
            self._send(self.core.apply_modes(list(body.get("modes") or self.cfg.get("modes") or []), now))
            if was_offline:
                self.log.info("guardian_back_online")
        else:
            raise ValueError(f"unknown control op {op!r}")
        self.log.info("control", op=op, modes=body.get("modes", body.get("mode")))
