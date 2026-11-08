"""Live timeline screen (rule R3): reading -> Guardian verdict -> Brain decision -> actuator action, timestamped.

    python timeline\\timeline.py          then open http://127.0.0.1:8002

It only LISTENS (MQTT city/#, guardian/#, brain/#) and shows what the layers say. It sends nothing.
Broker address comes from twin/city.yaml, so nothing is hard-coded. Works with the stand-ins or the real layers
as long as they publish on the topics in twin/INTERFACE.md and the triplet's INTEGRATION.md.
"""
import argparse
import asyncio
import json
import os
import queue
import threading
import time

import paho.mqtt.client as mqtt
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = 1000
SHOWN_KINDS = {"water_level"}   # kinds whose normal readings appear on the timeline (faulty ones always do)
READING_EVERY_S = 5          # a calm sensor is shown at most once per this many seconds

history, listeners = [], []
lock = threading.Lock()
last_shown = {}


def to_row(topic, m):
    """Turn one message into a timeline row, or None when it is not worth showing."""
    t = m.get("type")
    ts = m.get("ts", "")
    if t == "reading":
        sid = m.get("sensor_id")
        if m.get("kind") == "emergency_call":
            return dict(layer="twin", ts=ts, sid=sid, text="emergency call: %s" % m.get("value"))
        fault = m.get("fault")
        flagged = bool(fault)
        if not flagged and m.get("kind") not in SHOWN_KINDS:
            return None            # routine readings of other kinds would drown the story; the Guardian verdicts cover them
        now = time.time()
        key = (sid, fault.get("id") if isinstance(fault, dict) else None)
        if now - last_shown.get(key, 0) < READING_EVERY_S:
            return None
        last_shown[key] = now
        txt = "%s = %s %s" % (sid, m.get("value"), m.get("unit", ""))
        if flagged:
            txt += "   [labelled fault: %s]" % (fault.get("kind") if isinstance(fault, dict) else fault)
        if m.get("quarantined"):
            txt += "   [quarantined]"
        return dict(layer="twin", ts=ts, sid=sid, text=txt, kind=m.get("kind"), value=m.get("value"), flag=flagged)
    if t == "fault_event":
        return dict(layer="twin", ts=ts, sid=m.get("sensor_id"), flag=True,
                    text="fault %s: %s on %s" % (m.get("event"), m.get("kind"), m.get("sensor_id")))
    if t == "verdict":
        bad = m.get("status") != "trusted"
        status = str(m.get("status")).upper()
        reasons = " - " + "; ".join(m.get("reasons", [])[:2]) if m.get("reasons") else ""
        if topic == "guardian/verify":          # the answer to a Brain's POST /verify (the gate)
            return dict(layer="guardian", ts=ts, sid=m.get("sensor_id"), bad=bad,
                        text="verify %s -> %s trust=%s%s" % (m.get("verdict_id"), status, m.get("trust"), reasons))
        return dict(layer="guardian", ts=ts, sid=m.get("sensor_id"), bad=bad, verdict=True,
                    text="%s %s trust=%s%s" % (m.get("sensor_id"), status, m.get("trust"), reasons))
    if t == "alert":                            # quarantined / released / watcher, from the Guardian
        return dict(layer="guardian", ts=ts, sid=m.get("sensor_id") or m.get("actuator_id"), bad=True,
                    text="ALERT %s %s %s" % (m.get("event"), m.get("sensor_id") or "", "; ".join(m.get("reasons", [])[:1])))
    if t == "decision":
        extra = {k: m[k] for k in ("level_m", "actuator", "args", "tier", "verdict", "trust", "verdict_ref", "request_id",
                                   "ok", "state", "why") if m.get(k) is not None}
        return dict(layer="brain", ts=ts, sid=m.get("sensor_id"),
                    bad=m.get("event") in ("action_blocked", "action_failed", "command_failed", "operator_request", "operator_alert"),
                    text="%s %s %s" % (m.get("event"), m.get("sensor_id") or "", json.dumps(extra) if extra else ""))
    if t == "feedback":
        extra = ""
        if m.get("unit_id"):                    # dispatch_emergency: which unit, and when it arrives
            extra = " unit=%s eta=%ss" % (m.get("unit_id"), m.get("eta_s"))
        if m.get("sensor_id"):                  # quarantine_device
            extra = " sensor=%s" % m.get("sensor_id")
        return dict(layer="actuator", ts=ts, sid=m.get("actuator_id"), bad=not m.get("ok"),
                    text="%s -> %s (ok=%s) %s%s" % (m.get("actuator_id"), m.get("state"), m.get("ok"), m.get("detail", ""), extra),
                    actuator=m.get("actuator_id"), state=m.get("state"))
    return None


def publish_row(row):
    row["seen"] = time.time()
    with lock:
        history.append(row)
        del history[:-HISTORY]
        for q in list(listeners):
            q.put(row)


def start_mqtt(cfg):
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    if cfg["mqtt"].get("username"):
        c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password", ""))

    def on_connect(cl, u, f, rc, *r):
        for tp in ("city/#", "guardian/#", "brain/#"):
            cl.subscribe(tp)

    def on_message(cl, u, msg):
        try:
            m = json.loads(msg.payload)
        except ValueError:
            return
        row = to_row(msg.topic, m)
        if row:
            publish_row(row)

    c.on_connect, c.on_message = on_connect, on_message
    c.connect(cfg["mqtt"]["host"], cfg["mqtt"]["port"], 30)
    c.loop_start()


app = FastAPI(title="Timeline")


@app.get("/", response_class=HTMLResponse)
def page():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/history")
def get_history():
    with lock:
        return list(history)


@app.get("/stream")
async def stream():
    q = queue.Queue()
    with lock:
        listeners.append(q)

    async def gen():
        try:
            while True:
                try:
                    row = q.get_nowait()
                    yield "data: %s\n\n" % json.dumps(row)
                except queue.Empty:
                    await asyncio.sleep(0.2)
                    yield ": keepalive\n\n" if int(time.time()) % 15 == 0 else ""
        finally:
            with lock:
                listeners.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--twin-config", default=os.path.join(HERE, "..", "twin", "city.yaml"))
    ap.add_argument("--port", type=int, default=8002)
    a = ap.parse_args()
    with open(a.twin_config, encoding="utf-8-sig") as f:
        cfg = yaml.safe_load(f)
    for var, key in (("MQTT_HOST", "host"), ("MQTT_PORT", "port"), ("MQTT_USERNAME", "username"), ("MQTT_PASSWORD", "password")):
        if os.environ.get(var):          # same variables as the other services (.env.example, docker-compose.yml)
            cfg["mqtt"][key] = int(os.environ[var]) if key == "port" else os.environ[var]
    start_mqtt(cfg)
    print("Timeline on http://127.0.0.1:%d" % a.port)
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
