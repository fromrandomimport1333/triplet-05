"""Twin mock service: the simulator on MQTT.

Independent of MQTT like the Brain's input module: `handle()` takes (topic, payload, retained)
and everything goes out through `publish(topic, payload, qos, retain)`, so it is testable
without a broker.

In:   brain/cmd/{target_id}   (prefix from topics.cmd) command from the Brain, sealed with the twin-brain key
      twin/control            test control (trigger / reset / contain / faults / step), same key
Out:  twin/meta, twin/status, city/{zone}/sensor/{type}/{id}, city/{zone}/call,
      twin/actuator/{id}, twin/unit/{id}, every payload sealed with the twin-brain key
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from src.common import config
from src.common.envelope import EnvelopeError, parse_envelope, seal, verify_hmac
from src.common.log import EventLogger

from .city import load_city
from .core import Fault, SimConfig, Twin
from .messages import Mapper

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain


# Settings a `reset` control message may change (a scenario runner starts each run this way).
# They stay until the next reset that names them, so a runner sends all of them every time.
RESET_OVERRIDES = ("seed", "scenario", "scenario_zone", "scenario_after_ticks", "rush_hour", "calls")


def build_sim_config(cfg: dict[str, Any], now: float, run_id: str | None = None,
                     overrides: dict[str, Any] | None = None) -> SimConfig:
    sim = {**cfg["sim"], **(overrides or {})}
    epoch = datetime.fromtimestamp(now, timezone.utc) if sim.get("epoch", "now") == "now" \
        else datetime.fromisoformat(str(sim["epoch"]))
    return SimConfig(
        seed=int(sim["seed"]), run_id=run_id or str(sim["run_id"]), epoch=epoch, tick_s=float(sim["tick_s"]),
        command_delay_ticks=int(sim["command_delay_ticks"]), noise=float(sim["noise"]),
        travel_ticks=int(sim["travel_ticks"]), scene_ticks=int(sim["scene_ticks"]),
        scenario=str(sim["scenario"]), scenario_after_ticks=int(sim["scenario_after_ticks"]),
        clock=str(sim.get("clock", "wall")), scenario_zone=sim.get("scenario_zone") or None,
        city=load_city(config.resolve(sim["city"])) if sim.get("city") else None,
        slow_ack_s=float(sim.get("slow_ack_s", 5)), actuator_modes=cfg.get("actuator_modes") or {},
        rush_hour=sim.get("rush_hour", cfg.get("rush_hour")), call_texts=cfg.get("call_texts"),
        calls=sim.get("calls", cfg.get("calls")),
        faults=[Fault.from_dict(f) for f in cfg.get("faults") or []],
    )


class TwinService:
    def __init__(
        self,
        publish: Publisher,
        *,
        key: bytes,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        twin_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
    ):
        self.cfg = twin_cfg or config.load("twin-mock")
        self.topics_cfg = topics_cfg or config.load("topics")
        self._publish = publish
        self._key = key
        self.clock = clock
        self.log = logger or EventLogger("twin-mock")
        self._lock = threading.RLock()
        sim = self.cfg["sim"]
        self._resets = 0
        self._overrides: dict[str, Any] = {}     # from `reset` messages; the loaded config is never changed
        self.run_id = self._new_run_id()
        self.core = Twin(build_sim_config(self.cfg, clock(), self.run_id))
        self.mapper = Mapper(self.topics_cfg, self.run_id, self._now_iso)
        cmd_topic = self.cfg["topics"]["cmd"]            # e.g. brain/cmd/+ : commands arrive on <prefix><target_id>
        if not cmd_topic.endswith("/+"):
            raise ValueError(f"topics.cmd must end with '/+', got {cmd_topic!r}")
        self._cmd_prefix = cmd_topic[:-1]
        meta = self.core.meta()                          # the devices never change during a run: build the set once
        self._known_targets = {a["actuator_id"] for a in meta["actuators"]} | {u["unit_id"] for u in meta["units"]}
        self.max_age = float(sim["max_command_age_s"])
        self.refused = 0
        self.paused = False            # `offline` control op: the Twin goes down (scenario B13)

    def _new_run_id(self) -> str:
        """Unique per start and per reset, so reading ids never repeat inside the Brain's replay window."""
        base = self.cfg["sim"]["run_id"]
        base = f"tw{int(self.clock())}" if base == "auto" else str(base)
        return base if self._resets == 0 else f"{base}r{self._resets}"

    # --- helpers --------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _send(self, topic: str, body: dict[str, Any], qos: int, retain: bool, tamper: list[str] | None = None) -> None:
        key = b"wrong-key-" + self._key if tamper and "bad_hmac" in tamper else self._key
        self._publish(topic, seal(body, key), qos, retain)

    def _send_outputs(self, outputs: list[dict[str, Any]]) -> None:
        for out in outputs:
            topic, body, qos, retain = self.mapper.map(out)
            self._send(topic, body, qos, retain, out.get("tamper"))

    def status_payload(self, status: str) -> str:
        """twin/status body, sealed (used for the Last Will and on every connect)."""
        return seal(self.mapper.status(status)[1], self._key)

    def subscriptions(self) -> list[tuple[str, int]]:
        return [(self.cfg["topics"]["cmd"], 1), (self.cfg["topics"]["control"], 1)]

    # --- publishing -----------------------------------------------------------------

    def on_connect(self) -> None:
        """After every (re)connect: online, then the city description (retained), then states."""
        with self._lock:
            if self.paused:            # `offline` control: stay offline across reconnects
                self._send(*self.mapper.status("offline"))
                return
            for message in (self.mapper.status("online"), self.mapper.meta(self.core.meta())):
                self._send(*message)
            self._send_outputs(self.core.snapshot())
            self.log.info("twin_online", run_id=self.run_id, scenario=self.cfg["sim"]["scenario"])

    def tick(self) -> None:
        """One simulation step; publishes the readings, calls, acks and unit states of the tick."""
        with self._lock:
            if self.paused:
                return
            self._send_outputs(self.core.step(now=self.clock()))

    # --- inputs ---------------------------------------------------------------------

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        """Process one incoming message. Never raises."""
        if retained:
            return
        try:
            with self._lock:
                if topic == self.cfg["topics"]["control"]:
                    self._control(self._open(payload))
                elif topic.startswith(self._cmd_prefix) and self.paused:
                    self._refuse(topic, "twin offline (control op)")
                elif topic.startswith(self._cmd_prefix):
                    self._command(topic.removeprefix(self._cmd_prefix), self._open(payload))
        except (EnvelopeError, ValueError, KeyError, TypeError) as e:
            self._refuse(topic, str(getattr(e, "detail", e)))
        except Exception as e:  # a bug must not stop the loop
            self.log.error("twin_internal_error", topic=topic, error=repr(e))

    def _open(self, payload: bytes | str) -> dict[str, Any]:
        env = parse_envelope(payload)
        verify_hmac(env, self._key, "twin")
        body = json.loads(env.body)
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
        return body

    def _refuse(self, topic: str, reason: str) -> None:
        self.refused += 1
        self.log.warning("message_refused", topic=topic, reason=reason)

    def _command(self, topic_target: str, body: dict[str, Any]) -> None:
        if body.get("source") != "brain":
            raise ValueError("command source must be brain")
        ts = datetime.fromisoformat(str(body["timestamp"]))
        if ts.tzinfo is None or abs(ts.timestamp() - self.clock()) > self.max_age:
            raise ValueError(f"command timestamp outside +-{self.max_age:g} s")
        target = body.get("target_id", topic_target)
        if target != topic_target or target not in self._known_targets:
            raise ValueError(f"unknown or mismatching target {target!r}")
        if not isinstance(body.get("id"), str) or not isinstance(body.get("action"), str):
            raise ValueError("command needs string `id` and `action`")
        outputs = self.core.submit({"command_id": body["id"], "target_id": target, "action": body["action"],
                                    "parameters": body.get("parameters") or {}})
        self.log.info("command_received", correlation_id=body["id"], target_id=target, action=body["action"],
                      acknowledged=bool(outputs))
        self._send_outputs(outputs)

    def _control(self, body: dict[str, Any]) -> None:
        op = body["op"]
        if op == "trigger":
            self.core.trigger(body["scenario"], body.get("zone"))
        elif op == "call":
            self.core.inject_call(body["zone_id"], body["category"], body["text"])
        elif op == "contain":
            self.core.contain(body)
        elif op == "step":
            self._send_outputs(self.core.step(now=self.clock()))
        elif op == "faults":
            if "faults" not in body and "actuator_modes" not in body:
                raise ValueError("faults needs `faults` and / or `actuator_modes`")
            if "faults" in body:
                self.core.config.faults = [Fault.from_dict(f) for f in body["faults"]]
            if "actuator_modes" in body:
                self.core.set_actuator_modes(body["actuator_modes"])
        elif op == "offline":
            # the Twin goes down: status offline, then no readings, calls or acks (the city is frozen)
            # until `online` or `reset`; commands received meanwhile get no answer
            self.paused = True
            self._send(*self.mapper.status("offline"))
        elif op == "online":
            if self.paused:
                self.paused = False
                self.on_connect()
        elif op == "reset":
            self.paused = False
            self._resets += 1
            self.run_id = self._new_run_id()
            self._overrides.update({k: body[k] for k in RESET_OVERRIDES if k in body})
            self.core.reset(build_sim_config(self.cfg, self.clock(), self.run_id, self._overrides))
            self._send(*self.mapper.status("offline"))   # a restart: the Brain then restarts its seq counters
            self.mapper = Mapper(self.topics_cfg, self.run_id, self._now_iso)
            self.on_connect()
        else:
            raise ValueError(f"unknown control op {op!r}")
        self.log.info("control", op=op)
