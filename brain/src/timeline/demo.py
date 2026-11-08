"""Rehearse the screen without a broker: python -m src.timeline.demo [--port 8091]

Feeds a scripted flood chain (reading -> Guardian verdict -> incident -> LLM -> decision ->
command -> ack -> effect, plus a refusal, a quarantine, a rejected message and safe mode) into
the real TimelineService and serves it. For checking the projector layout only: the run log it
writes goes to a temporary folder, never to logs/.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from pathlib import Path

import yaml

from ..common import config
from ..common.envelope import seal, seal_multi
from ..common.log import EventLogger
from .runlog import RunLog
from .server import serve
from .service import TimelineService, iso

KEYS = {"twin": b"demo-twin-key", "guardian": b"demo-guardian-key"}


class Feeder:
    def __init__(self, svc: TimelineService, city_file: Path):
        self.svc = svc
        self.n = 0
        city = yaml.safe_load(city_file.read_text(encoding="utf-8"))
        self.sensors = []
        for z in city["zones"]:
            nn = z["zone_id"][-2:]
            for t, st in city["sensor_types"].items():
                s = {"sensor_id": f"{st['prefix']}-{nn}", "zone_id": z["zone_id"], "type": t, "unit": st["unit"],
                     "min": st["min"], "max": st["max"], "baseline": st["baseline"]}
                if "alert" in st:
                    s["alert"] = st["alert"]
                self.sensors.append(s)
        self.internal("brain/internal/meta", {"correlation_id": "twin-meta", "zones": city["zones"],
                                              "sensors": [{k: v for k, v in s.items() if k != "baseline"}
                                                          for s in self.sensors],
                                              "actuators": city["actuators"], "units": city["units"]}, True)

    def nid(self, p: str) -> str:
        self.n += 1
        return f"{p}-{self.n:06d}"

    def twin(self, topic: str, body: dict) -> None:
        self.svc.handle(topic, seal({"id": self.nid("twin"), "timestamp": iso(time.time()), "source": "twin", **body},
                                    KEYS["twin"]))

    def guardian(self, topic: str, body: dict) -> None:
        self.svc.handle(topic, seal({"id": self.nid("gd"), "timestamp": iso(time.time()), "source": "guardian",
                                     **body}, KEYS["guardian"]))

    def brain(self, topic: str, body: dict) -> None:
        self.svc.handle(topic, seal_multi({"id": self.nid("out"), "timestamp": iso(time.time()), "source": "brain",
                                           **body}, KEYS))

    def internal(self, topic: str, body: dict, retained: bool = False) -> None:
        self.svc.handle(topic, json.dumps({"id": self.nid("in"), "timestamp": iso(time.time()), **body}), retained)

    def readings(self, water01: float) -> None:
        for s in self.sensors:
            v = water01 if s["sensor_id"] == "water-01" else s["baseline"] * (1 + random.uniform(-.02, .02))
            self.internal("brain/internal/reading", {"correlation_id": "twin-000182", "sensor_id": s["sensor_id"],
                                                     "zone_id": s["zone_id"], "type": s["type"], "value": v,
                                                     "unit": s["unit"]})

    def script(self, pace_s: float) -> None:
        cid = "twin-000182"
        steps = [
            lambda: self.twin("twin/status", {"status": "online"}),
            lambda: self.guardian("guardian/status", {"status": "online"}),
            lambda: self.brain("brain/status", {"status": "online"}),
            lambda: self.internal("brain/internal/llm-status", {"status": "online", "model": "qwen2.5-coder:7b"}),
            lambda: self.internal("brain/control/run", {"run_id": "demo-1", "scenario_id": "B2-flood",
                                                        "action": "start"}),
            lambda: [self.readings(lv) for lv in (0.9, 1.2, 1.45, 1.6, 1.75)],
            lambda: self.guardian("guardian/verdict/water-01", {"sensor_id": "water-01", "verdict": "trusted",
                                                                "reason": "consistent", "trust_score": 0.97,
                                                                "correlation_id": cid}),
            lambda: self.twin("city/zone-01/call", {"call_id": "call-77", "zone_id": "zone-01", "category": "flood",
                                                    "text": "Water is coming into the port warehouses"}),
            lambda: self.internal("brain/internal/incident", {
                "correlation_id": cid, "incident_id": "inc-0031", "type": "flood", "zone_id": "zone-01",
                "status": "opened", "confidence": 0.82, "severity": "high", "verified": True,
                "evidence": [{"source_id": "water-01", "kind": "reading", "value": 1.75, "unit": "m", "trust": 0.97},
                             {"source_id": "call-77", "kind": "call", "category": "flood", "trust": 1.0}]}),
            lambda: self.brain("brain/event", {"event": "llm_proposal", "correlation_id": cid, "incident_id": "inc-0031",
                                               "summary": "LLM proposal for flood at zone-01: open valve-01, "
                                                          "priority signal-01"}),
            lambda: self.internal("brain/internal/proposal", {"correlation_id": cid, "incident_id": "inc-0031",
                                                              "origin": "rules", "justification": "standard action",
                                                              "actions": [{"target_id": "valve-01", "action": "open"}]}),
            lambda: self.internal("brain/internal/decision", {
                "correlation_id": cid, "incident_id": "inc-0031", "chosen_proposal": "llm-000012", "origin": "llm",
                "actions": [{"target_id": "valve-01", "action": "open", "params": {"opening": 0.8}},
                            {"target_id": "signal-01", "action": "priority"}],
                "score": 1.53, "alternatives": [{"proposal_id": "rules-000009", "origin": "rules", "score": 1.21}],
                "risk": "high", "mode": "normal",
                "explanation": "Water rising fast at the port: the valve drains the zone, the signal clears the route."}),
            lambda: self.brain("brain/cmd/valve-01", {"id": "brain-000042", "correlation_id": cid,
                                                      "target_id": "valve-01", "action": "open",
                                                      "parameters": {"opening": 0.8}}),
            lambda: self.brain("brain/event", {"event": "command_sent", "correlation_id": cid,
                                               "summary": "command brain-000042 sent: open valve-01 (flood at "
                                                          "zone-01 (Port)) [opening=0.8]"}),
            lambda: self.brain("brain/event", {"event": "action_refused", "correlation_id": cid,
                                               "summary": "REFUSED priority signal-01 (flood at zone-01 (Port)): "
                                                          "S4 same action on signal-01 12 s ago (cooldown 60 s)"}),
            lambda: self.twin("twin/actuator/valve-01", {"actuator_id": "valve-01", "correlation_id": "brain-000042",
                                                         "result": "done", "state": "open"}),
            lambda: self.brain("brain/event", {"event": "command_acknowledged", "correlation_id": cid,
                                               "summary": "valve-01 acknowledged brain-000042: open valve-01, "
                                                          "state open"}),
            lambda: self.readings(1.46),
            lambda: self.brain("brain/event", {"event": "effect_confirmed", "correlation_id": cid,
                                               "summary": "effect confirmed for open valve-01: water-01 fell "
                                                          "from 1.75 to 1.46"}),
            lambda: self.twin("twin/unit/unit-fire-02", {"unit_id": "unit-fire-02", "status": "en_route",
                                                         "zone_id": "zone-01"}),
            lambda: self.guardian("guardian/quarantine/valve-03", {"device_id": "valve-03", "state": "quarantined",
                                                                   "reason": "inconsistent"}),
            lambda: self.brain("brain/error", {"code": "E_AUTH", "topic": "city/zone-03/sensor/water_level/water-03",
                                               "message_id": "twin-000990", "detail": "HMAC does not match the body"}),
            lambda: self.internal("brain/internal/layer-status/guardian", {"layer": "guardian", "status": "offline",
                                                                           "reason": "no verdict for 30 s"}),
            lambda: self.brain("brain/event", {"event": "safe_mode_on",
                                               "summary": "SAFE MODE ON: Guardian offline, risky actions need "
                                                          "2 sensor types"}),
            lambda: self.brain("brain/alert", {"alert": "actuator_not_responding", "target_id": "grid-01",
                                               "correlation_id": "twin-000200",
                                               "detail": "brain-000043: no acknowledgement after 2 attempts (8 s)"}),
        ]
        for _ in range(20):
            self.readings(0.4 + random.uniform(-.01, .01))
            self.svc._spark_last.clear()
        for step in steps:
            step()
            self.svc._spark_last.clear()
            time.sleep(pace_s)


def main() -> None:
    ap = argparse.ArgumentParser(description="Timeline demo feed (no broker)")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--pace", type=float, default=0.4, help="seconds between scripted messages")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="timeline-demo-"))
    svc = TimelineService(hmac_keys=KEYS, logger=EventLogger("timeline-demo", None, "ERROR"),
                          runlog=RunLog(tmp, clock=time.time))
    srv, _ = serve(svc, config.load("timeline"), "127.0.0.1", args.port)
    print(f"demo timeline: http://127.0.0.1:{srv.server_address[1]}/  (run log in {tmp})", flush=True)
    feeder = Feeder(svc, config.resolve(config.load("twin-mock")["sim"]["city"]))
    feeder.script(args.pace)
    try:
        while True:
            time.sleep(1)
            feeder.readings(1.46 + random.uniform(-.02, .02))
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
