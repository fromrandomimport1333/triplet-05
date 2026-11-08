"""Twin mock: publishes signed Twin events (normal AND stuck / spoofed / tampered / replayed)
and implements Twin's side of the quarantine contract (INTERFACE.md §5).

Library use (tests, scenarios):  TwinMock(hmac_key, publish=bus.publish, ...)
CLI (live, against an MQTT broker): python -m mocks.twin_mock --mode normal --count 30
"""
from __future__ import annotations

import argparse
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from src.core.events import EVENT_SCHEMA_VERSION, VerdictSigner, iso, sign_event

SENSOR_UNITS = {"water_level": "m", "temperature": "C", "traffic_flow": "veh/h",
                "air_quality_pm25": "ug/m3", "power_load": "kW", "rainfall": "mm/h"}


class TwinMock:
    def __init__(self, hmac_key: str, publish, topic_prefix: str = "twin/events",
                 guardian_pubkey_pem: str | None = None, topic_acks: str = "twin/acks/quarantine",
                 start: datetime | None = None, seq_base: int = 0) -> None:
        self.key, self.publish, self.prefix = hmac_key, publish, topic_prefix
        self.pubkey = guardian_pubkey_pem
        self.topic_acks = topic_acks
        # simulated clock starts in the past so accelerated scenarios never hit "future ts"
        self.clock = start or (datetime.now(timezone.utc) - timedelta(hours=6))
        self.seq_base = seq_base  # live mode: time-based so re-runs stay monotonic
        self.seq: dict[str, int] = {}
        self.quarantined: set[str] = set()
        self.refused: list[dict] = []
        self.executed: list[dict] = []
        self.rejected_control: list[dict] = []

    # ---------------- event generation ----------------
    def make_event(self, device_id: str, value: float, sensor_type: str = "water_level",
                   ts: datetime | None = None, *, sign: bool = True) -> dict:
        self.seq[device_id] = self.seq.get(device_id, self.seq_base) + 1
        evt = {"schema_version": EVENT_SCHEMA_VERSION, "event_id": f"{device_id}-{uuid.uuid4().hex[:12]}",
               "seq": self.seq[device_id], "device_id": device_id, "sensor_type": sensor_type,
               "value": float(value), "unit": SENSOR_UNITS.get(sensor_type, "?"),
               "ts": iso(ts or self.clock), "signature": ""}
        return sign_event(evt, self.key) if sign else evt

    def emit(self, evt: dict) -> dict:
        self.publish(f"{self.prefix}/{evt['device_id']}", evt)
        return evt

    def tick(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def normal(self, device_id: str, base: float = 1.2, noise: float = 0.01, step_s: float = 10,
               sensor_type: str = "water_level") -> dict:
        self.tick(step_s)
        return self.emit(self.make_event(device_id, round(base + random.uniform(-noise, noise), 4), sensor_type))

    def stuck(self, device_id: str, value: float = 1.2, step_s: float = 10) -> dict:
        self.tick(step_s)
        return self.emit(self.make_event(device_id, value))

    def spoofed(self, device_id: str, value: float = 8.5, step_s: float = 5) -> dict:
        """Compromised sensor: properly signed but physically impossible reading."""
        self.tick(step_s)
        return self.emit(self.make_event(device_id, value))

    def tampered(self, device_id: str, value: float = 8.5, step_s: float = 5) -> dict:
        """MITM: valid event whose value is altered in transit (signature no longer matches)."""
        self.tick(step_s)
        evt = self.make_event(device_id, 1.2)
        evt["value"] = float(value)
        return self.emit(evt)

    def replay(self, evt: dict) -> dict:
        return self.emit(dict(evt))

    # ---------------- quarantine contract (Twin side) ----------------
    def on_control(self, topic: str, msg: dict) -> None:
        """Handle guardian/control/<device_id>. Unsigned/invalid messages are ignored and logged."""
        if self.pubkey is None or not VerdictSigner.verify(msg, self.pubkey):
            self.rejected_control.append(msg)
            return
        dev = msg["device_id"]
        if msg["action"] == "quarantine":
            self.quarantined.add(dev)
        elif msg["action"] == "release":
            self.quarantined.discard(dev)
        self.publish(f"{self.topic_acks}/{dev}", {
            "schema_version": "1.0", "device_id": dev, "action": msg["action"],
            "state": "quarantined" if dev in self.quarantined else "active",
            "control_sig": msg["sig"]["value"], "ack_at": iso(datetime.now(timezone.utc))})

    def execute_command(self, device_id: str, command: str, issued_by: str = "brain") -> dict:
        """Actuator command entry point. MUST refuse commands for quarantined devices."""
        rec = {"device_id": device_id, "command": command, "issued_by": issued_by}
        if device_id in self.quarantined:
            self.refused.append(rec)
            return {"accepted": False, "error": {"code": "DEVICE_QUARANTINED",
                                                 "message": f"{device_id} is quarantined by Guardian"}}
        self.executed.append(rec)
        return {"accepted": True}


def _cli() -> None:  # pragma: no cover - live mode
    import json

    from src.config import load_settings
    from src.mqtt.publisher import make_mqtt_client

    ap = argparse.ArgumentParser(description="Publish fake Twin events to MQTT")
    ap.add_argument("--mode", choices=["normal", "stuck", "spoof", "tamper", "replay"], default="normal")
    ap.add_argument("--device", default="wl-sensor-01")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--interval", type=float, default=1.0, help="wall-clock seconds between events")
    ap.add_argument("--sim-step", type=float, default=10.0, help="simulated seconds between events")
    a = ap.parse_args()
    s = load_settings()
    c = make_mqtt_client(s, "twin-mock")
    c.connect(s.mqtt_host, s.mqtt_port)
    c.loop_start()
    pub = lambda t, p: c.publish(t, json.dumps(p), qos=1)  # noqa: E731
    tw = TwinMock(s.twin_event_hmac_key, pub, s.topic_in)
    for _ in range(10):  # baseline
        tw.normal(a.device, step_s=a.sim_step)
        time.sleep(a.interval)
    last = None
    for _ in range(a.count):
        if a.mode == "normal":
            last = tw.normal(a.device, step_s=a.sim_step)
        elif a.mode == "stuck":
            last = tw.stuck(a.device, step_s=a.sim_step)
        elif a.mode == "spoof":
            last = tw.spoofed(a.device)
        elif a.mode == "tamper":
            last = tw.tampered(a.device)
        elif a.mode == "replay":
            last = last or tw.normal(a.device)
            tw.replay(last)
        print(json.dumps(last))
        time.sleep(a.interval)
    c.loop_stop()


if __name__ == "__main__":  # pragma: no cover
    _cli()
