"""Run one Test Book scenario with ONE command and leave a log file in logs/ (rule R2).

    python scenarios\\run_scenario.py t3                  run scenario T3 against our stand-ins
    python scenarios\\run_scenario.py t3 --layers real    the same, against the partner teams' Brain and Guardian
    python scenarios\\run_scenario.py all                 run every scenario, one after the other
    python scenarios\\run_scenario.py --list              list the scenarios
    python scenarios\\run_scenario.py --testbook       write twin/TESTBOOK.md from the scenario definitions

What must be running first: the MQTT broker, the Twin, the Guardian and the Brain (stand-ins or the real layers),
for example with start.ps1 or docker compose up. Each run writes
  logs/<id>-<date>-<time>.log    a timestamped timeline across the layers, then PASS/FAIL for every expected result
  logs/<id>-<date>-<time>.json   a compact summary (faults, verdicts, decisions, reading counts) used by scripts/kpis.py
The exit code is 0 when everything passed. The scenario DEFINITIONS are in scenarios.py; this file is the machinery.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import httpx
import paho.mqtt.client as mqtt
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import scenarios as defs  # noqa: E402


def wall():
    return time.time()


def fmt_time(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def iso_to_epoch(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


class Context:
    """What a scenario can do: act on the Twin, wait for things to happen, and record checks."""

    def __init__(self, cfg, scenario, log_path):
        self.cfg, self.sc = cfg, scenario
        self.layers = cfg.get("layers", "mocks")
        self.twin = cfg["twin_url"].rstrip("/")
        self.guardian = cfg["guardian_url"].rstrip("/")
        self.brain = (cfg.get("brain_url") or "").rstrip("/")
        self.hmac_key = cfg["hmac_key"]
        self.headers = {"X-API-Key": cfg["twin_key"]}
        self.gheaders = {"X-API-Key": cfg["guardian_key"]}
        self.events = []                 # every message seen: dict(t, topic, msg)
        self.lock = threading.Lock()
        self.checks = []
        self.marks = {}                  # named moments, for the KPIs (e.g. "incident" = real incident starts)
        self.attacks = []                # messages the scenario itself forged or replayed: (sensor, start, end)
        self.t_start = None
        self.log_path = log_path
        self.logf = open(log_path, "w", encoding="utf-8")
        self.last_reading_logged = {}
        self.t0 = wall()
        self.client = self.connect_mqtt()

    # ---------- logging ----------
    def line(self, layer, text, t=None):
        s = "%s [%-9s] %s" % (fmt_time(t or wall()), layer, text)
        self.logf.write(s + "\n")
        self.logf.flush()
        print(s)

    def step(self, text):
        self.line("SCENARIO", text)

    def mark(self, name, t=None):
        if name not in self.marks:
            self.marks[name] = t or wall()
            self.line("SCENARIO", "mark: %s" % name, self.marks[name])

    # ---------- MQTT ----------
    def connect_mqtt(self):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            c = mqtt.Client()
        if self.cfg.get("mqtt_user"):
            c.username_pw_set(self.cfg["mqtt_user"], self.cfg.get("mqtt_password", ""))
        c.on_connect = lambda cl, u, f, rc, *r: [cl.subscribe(t) for t in ("city/#", "guardian/#", "brain/#")]
        c.on_message = self.on_message
        c.connect(self.cfg["mqtt_host"], int(self.cfg["mqtt_port"]), 30)
        c.loop_start()
        return c

    def on_message(self, client, userdata, message):
        try:
            msg = json.loads(message.payload)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        t = wall()
        with self.lock:
            self.events.append({"t": t, "topic": message.topic, "msg": msg})
        self.describe(t, message.topic, msg)

    def describe(self, t, topic, m):
        typ = m.get("type")
        if typ == "fault_event":
            self.line("TWIN", "fault %s on %s (%s)%s" % (m["event"], m["sensor_id"], m["kind"],
                      " value=%s" % m["value"] if m.get("value") is not None else ""), t)
        elif typ == "feedback":
            extra = " unit=%s eta=%ss" % (m.get("unit_id"), m.get("eta_s")) if m.get("unit_id") else ""
            self.line("ACTUATOR", "%s -> %s ok=%s %s%s" % (m["actuator_id"], m["state"], m["ok"], m.get("detail", ""), extra), t)
        elif typ == "verdict" and topic.startswith("guardian/verdicts/"):
            self.line("GUARDIAN", "%s %s trust=%s %s" % (m.get("sensor_id"), m["status"].upper(), m.get("trust"),
                                                         "; ".join(m.get("reasons", [])[:2])), t)
        elif typ == "verdict":
            self.line("GUARDIAN", "verify -> %s trust=%s %s" % (m["status"].upper(), m.get("trust"), m.get("verdict_id")), t)
        elif typ == "alert":
            self.line("GUARDIAN", "ALERT %s %s %s" % (m.get("event"), m.get("sensor_id") or m.get("actuator_id") or "",
                                                     "; ".join(m.get("reasons", [])[:1])), t)
        elif typ == "decision":
            extra = {k: v for k, v in m.items() if k in ("level_m", "actuator", "args", "ok", "state", "verdict", "trust",
                                                         "tier", "verdict_ref", "request_id", "why")}
            self.line("BRAIN", "%s %s %s" % (m["event"], m.get("sensor_id") or "", json.dumps(extra) if extra else ""), t)
        elif typ == "reading" and m.get("sensor_id") in self.sc.get("watch", []):
            last = self.last_reading_logged.get(m["sensor_id"], 0)
            if t - last >= 3:
                self.last_reading_logged[m["sensor_id"]] = t
                self.line("TWIN", "%s = %s %s%s" % (m["sensor_id"], m["value"], m["unit"],
                                                   " (quarantined)" if m.get("quarantined") else ""), t)

    def find(self, since, topic_prefix=None, **match):
        """Messages seen after `since` whose fields equal `match` (a callable value is used as a test)."""
        out = []
        with self.lock:
            snapshot = list(self.events)
        for e in snapshot:
            if e["t"] < since or (topic_prefix and not e["topic"].startswith(topic_prefix)):
                continue
            ok = True
            for k, want in match.items():
                got = e["msg"].get(k)
                matched = want(got) if callable(want) else got == want
                if not matched:
                    ok = False
                    break
            if ok:
                out.append(e)
        return out

    def wait_for(self, label, predicate, timeout=60, poll=0.2):
        """Wait until predicate() returns something truthy. Returns it, or None on timeout."""
        end = wall() + timeout
        while wall() < end:
            r = predicate()
            if r:
                return r
            time.sleep(poll)
        self.line("SCENARIO", "timed out after %ss waiting for: %s" % (timeout, label))
        return None

    # ---------- checks ----------
    def check(self, name, ok, detail="", only=None):
        """only="mocks" or "real": the check applies to that profile only, and is SKIPPED for the other."""
        if only and only != self.layers:
            self.line("CHECK", "SKIP  %s  (applies to --layers %s only)" % (name, only))
            return
        self.checks.append((name, bool(ok), detail))
        self.line("CHECK", "%s  %s%s" % ("PASS" if ok else "FAIL", name, "  (%s)" % detail if detail else ""))

    # ---------- acting on the Twin ----------
    def api(self, method, path, **kw):
        return httpx.request(method, self.twin + path, headers=self.headers, timeout=5, **kw)

    def rain(self, mm_h):
        self.step("rain %s mm/h" % mm_h)
        self.api("POST", "/scenario/rain/%s" % mm_h)

    def conditions(self, **body):
        self.step("conditions %s" % json.dumps(body))
        return self.api("POST", "/scenario/conditions", json=body)

    def fault(self, sensor, kind, value=None, duration_s=None):
        body = {"sensor_id": sensor, "kind": kind}
        if value is not None:
            body["value"] = value
        if duration_s:
            body["duration_s"] = duration_s
        self.step("inject fault: %s on %s%s" % (kind, sensor, " = %s" % value if value is not None else ""))
        r = self.api("POST", "/faults", json=body)
        return r.json() if r.status_code == 200 else None

    def reading(self, sid):
        r = httpx.get("%s/sensors/%s" % (self.twin, sid), timeout=5)
        return r.json()["value"] if r.status_code == 200 else None

    def truth(self, sid):
        r = self.api("GET", "/truth/%s" % sid)
        return r.json().get("true_value") if r.status_code == 200 else None

    def actuator_state(self, aid):
        r = self.api("GET", "/actuators/%s" % aid)
        return r.json().get("state") if r.status_code == 200 else None

    def quarantined(self):
        try:
            return httpx.get(self.twin + "/quarantine", timeout=5).json()
        except (httpx.HTTPError, ValueError):
            return None

    def verdict(self, sid):
        try:
            r = httpx.get("%s/verdict/%s" % (self.guardian, sid), headers=self.gheaders, timeout=3)
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def guardian_up(self):
        try:
            return httpx.get(self.guardian + "/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def untrusted_now(self):
        try:
            r = httpx.get(self.guardian + "/verdicts", headers=self.gheaders, timeout=3)
            return [v["sensor_id"] for v in r.json() if v["status"] != "trusted"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    # ---------- layers going down and up (scenario T6) ----------
    def kill_guardian(self):
        how = self.cfg.get("guardian_kill", "port")
        if isinstance(how, list):
            subprocess.run([x.replace("{python}", sys.executable) for x in how], cwd=ROOT, timeout=60)
            return True
        import psutil
        port = int(self.guardian.rsplit(":", 1)[-1].split("/")[0])
        killed = []
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                p = psutil.Process(c.pid)
                for proc in [p] + ([p.parent()] if p.parent() and "python" in (p.parent().name() or "").lower() else []):
                    try:
                        proc.kill()
                        killed.append(proc.pid)
                    except psutil.Error:
                        pass
        self.step("killed the Guardian (port %s, pids %s)" % (port, killed))
        return bool(killed)

    def start_guardian(self):
        cmd = self.cfg.get("guardian_start")
        if not cmd:
            self.step("guardian_start is not configured: restart the Guardian by hand")
            return False
        os.makedirs(os.path.join(ROOT, "logs", "services"), exist_ok=True)
        out = open(os.path.join(ROOT, "logs", "services", "guardian_restarted.log"), "a")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([x.replace("{python}", sys.executable) for x in cmd], cwd=ROOT, stdout=out, stderr=out,
                         creationflags=flags if os.name == "nt" else 0, start_new_session=os.name != "nt")
        ok = self.wait_for("Guardian back", self.guardian_up, 30)
        self.step("Guardian restarted" if ok else "Guardian did not come back")
        return bool(ok)

    def settle(self, devices):
        """Return the city to a calm state: Twin baseline (no rain, no faults, water low, valve closed, road open),
        quarantines released through the Guardian, and every sensor trusted."""
        self.step("settling: Twin reset to its calm baseline, quarantines released")
        if not self.guardian_up():
            self.start_guardian()
        self.api("POST", "/scenario/reset")
        for sid in self.quarantined() or []:
            try:
                httpx.post("%s/devices/%s/release" % (self.guardian, sid), headers=self.gheaders, timeout=5)
            except httpx.HTTPError:
                pass
        ok = self.wait_for("all sensors trusted", lambda: self.untrusted_now() == [], 90)
        time.sleep(2)
        return bool(ok)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()
        self.logf.close()

    # ---------- summary for the KPIs ----------
    def summary(self, sid, passed):
        """Compact JSON next to the log. Readings are not stored one by one; they are counted against the
        Guardian's verdict timeline: a reading is 'faulty' if its sensor had an injected fault (or a scenario
        attack) at that moment, 'healthy' otherwise; readings in the 15 s after a fault ends are not counted."""
        since = self.t_start or self.t0
        with self.lock:
            ev = [e for e in self.events if e["t"] >= since]
        faults, open_f = [], {}
        for e in ev:
            m = e["msg"]
            if m.get("type") == "fault_event":
                if m["event"] == "start":
                    open_f[m["fault_id"]] = {"sensor_id": m["sensor_id"], "kind": m["kind"], "start": e["t"], "end": None}
                elif m["fault_id"] in open_f:
                    open_f[m["fault_id"]]["end"] = e["t"]
        faults = list(open_f.values())
        windows = [(f["sensor_id"], f["start"], f["end"] or ev[-1]["t"] if ev else f["start"]) for f in faults] + \
                  [tuple(a) for a in self.attacks]
        verdicts = [(e["t"], e["msg"].get("sensor_id"), e["msg"].get("status")) for e in ev
                    if e["msg"].get("type") == "verdict" and e["topic"].startswith("guardian/verdicts/")]
        status = {}
        vi = 0
        counts = {"healthy": 0, "healthy_untrusted": 0, "healthy_suspicious": 0, "faulty": 0, "faulty_flagged": 0}
        readings = sorted([e for e in ev if e["msg"].get("type") == "reading" and e["msg"].get("kind") != "emergency_call"],
                          key=lambda e: e["t"])
        down = (self.marks.get("guardian_killed"), self.marks.get("guardian_back") or wall())
        for e in readings:
            while vi < len(verdicts) and verdicts[vi][0] <= e["t"] + 0.5:
                status[verdicts[vi][1]] = verdicts[vi][2]
                vi += 1
            if down[0] and down[0] <= e["t"] <= down[1]:
                continue                  # nobody was judging: T6 kills the Guardian on purpose
            s = e["msg"]["sensor_id"]
            st = status.get(s, "trusted")
            faulty = bool(e["msg"].get("fault")) or any(w[0] == s and w[1] <= e["t"] <= w[2] for w in windows)
            recovering = any(w[0] == s and w[2] < e["t"] <= w[2] + 15 for w in windows)
            if faulty:
                counts["faulty"] += 1
                counts["faulty_flagged"] += st != "trusted"
            elif not recovering and not e["msg"].get("quarantined"):
                counts["healthy"] += 1
                counts["healthy_untrusted"] += st == "untrusted"
                counts["healthy_suspicious"] += st == "suspicious"
        attacks = [{"sensor_id": a[0], "kind": "scenario_attack", "start": a[1], "end": a[2]} for a in self.attacks]
        for f in faults + attacks:
            first = next((t for t, s, st in verdicts if s == f["sensor_id"] and st != "trusted" and t >= f["start"]
                          and t <= (f["end"] or 1e18) + 5), None)
            f["flagged_after_s"] = round(first - f["start"], 2) if first else None
        decisions = [{"t": e["t"], **{k: e["msg"].get(k) for k in ("event", "sensor_id", "actuator", "verdict", "trust", "tier",
                                                                     "verdict_ref", "sensor_ids")}}
                     for e in ev if e["msg"].get("type") == "decision"]
        for d in decisions:
            if d["event"] == "command_sent":
                used = d.get("sensor_ids") or ([d["sensor_id"]] if d.get("sensor_id") else [])
                d["on_false_data"] = any(w[0] in used and w[1] <= d["t"] <= w[2] for w in windows)
        out = {"scenario": sid, "layers": self.layers, "passed": passed, "start": since, "end": wall(),
               "checks": [{"name": n, "ok": ok, "detail": dt} for n, ok, dt in self.checks],
               "marks": self.marks, "faults": faults, "attacks": attacks, "readings": counts,
               "decisions": decisions,
               "alerts": [{"t": e["t"], "event": e["msg"].get("event"), "id": e["msg"].get("sensor_id") or e["msg"].get("actuator_id")}
                          for e in ev if e["msg"].get("type") == "alert"],
               "feedback": [{"t": e["t"], "actuator": e["msg"].get("actuator_id"), "state": e["msg"].get("state"), "ok": e["msg"].get("ok"),
                             "cmd_id": e["msg"].get("cmd_id")} for e in ev if e["msg"].get("type") == "feedback"]}
        path = self.log_path[:-4] + ".json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        return path


def load_cfg(a):
    with open(a.twin_config, encoding="utf-8-sig") as f:
        t = yaml.safe_load(f)
    with open(a.config, encoding="utf-8-sig") as f:
        sc = yaml.safe_load(f)
    layers = a.layers or sc.get("default_layers", "mocks")
    p = dict(sc["profiles"][layers])
    env = os.environ.get
    cfg = {"layers": layers, "twin_url": env("TWIN_URL") or p["twin_url"], "guardian_url": env("GUARDIAN_URL") or p["guardian_url"],
           "brain_url": env("BRAIN_URL") or p.get("brain_url"), "guardian_key": env("GUARDIAN_API_KEY") or p["guardian_key"],
           "twin_key": env("TWIN_TEST_KEY") or t["api"].get("test_key", t["api"]["api_key"]),
           "hmac_key": env("TSYP_HMAC_KEY") or t["api"]["hmac_key"],
           "mqtt_host": env("MQTT_HOST") or t["mqtt"]["host"], "mqtt_port": int(env("MQTT_PORT") or t["mqtt"]["port"]),
           "mqtt_user": env("MQTT_USERNAME") or t["mqtt"].get("username"), "mqtt_password": env("MQTT_PASSWORD") or t["mqtt"].get("password")}
    for k in ("flood_threshold_m", "jam_sustain_s", "guardian_kill", "guardian_start"):
        cfg[k] = p.get(k)
    return cfg


def run_one(cfg, sid, log_dir):
    sc = defs.SCENARIOS[sid]
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "%s-%s.log" % (sid, datetime.now().strftime("%Y%m%d-%H%M%S")))
    ctx = Context(cfg, sc, path)
    ctx.line("SCENARIO", "%s: %s   layers: %s   (--layers %s)" % (sid.upper(), sc["name"], sc["layers"], ctx.layers))
    ctx.line("SCENARIO", "trigger: python scenarios\\run_scenario.py %s --layers %s" % (sid, ctx.layers))
    try:
        health = httpx.get(cfg["twin_url"] + "/health", timeout=3).json()
        if not ctx.guardian_up():
            ctx.start_guardian()
        ghealth = httpx.get(cfg["guardian_url"] + "/health", timeout=3).json()
        devices = httpx.get(cfg["twin_url"] + "/devices", timeout=5).json()
    except (httpx.HTTPError, ValueError) as e:
        ctx.check("Twin and Guardian are running", False, "cannot reach them: %s" % e)
        ctx.close()
        return False, path
    ctx.check("Twin and Guardian are running", True, "sim_time=%s, sensors judged by the Guardian=%s" %
              (health["sim_time"], ghealth.get("sensors_seen")))
    try:
        if ctx.settle(devices):
            ctx.t_start = wall()
            sc["run"](ctx)
        else:
            ctx.check("city is calm and every sensor trusted before the test", False, "could not settle: %s" % ctx.untrusted_now())
    except Exception as e:      # a scenario must always leave a log, even when it breaks
        ctx.check("scenario ran without an error", False, "%s: %s" % (type(e).__name__, e))
    finally:
        try:
            ctx.api("POST", "/scenario/reset")
            if not ctx.guardian_up():
                ctx.start_guardian()
        except httpx.HTTPError:
            pass
    passed = all(ok for _, ok, _ in ctx.checks)
    ctx.line("RESULT", "%s  (%d of %d checks passed)" % ("PASS" if passed else "FAIL",
                                                        sum(1 for _, ok, _ in ctx.checks if ok), len(ctx.checks)))
    ctx.summary(sid, passed)
    ctx.close()
    return passed, path


def write_testbook(path):
    out = ["# Test Book", "",
           "Generated from `scenarios/scenarios.py` by `python scenarios\\run_scenario.py --testbook`. Do not edit by hand.",
           "Each scenario starts with one command and writes `logs/<id>-<date>-<time>.log` (timeline and PASS/FAIL) and a `.json` summary.",
           "Add `--layers real` to run it against the partner teams' Brain and Guardian instead of the Twin team's stand-ins.",
           "", "| ID | Scenario | Phase 0 report (Table 10) | Layers |", "|---|---|---|---|"]
    for sid, sc in defs.SCENARIOS.items():
        out.append("| %s | %s | %s | %s |" % (sid.upper(), sc["name"], sc.get("pdf", "added"), sc["layers"]))
    out.append("")
    for sid, sc in defs.SCENARIOS.items():
        out += ["## %s: %s" % (sid.upper(), sc["name"]), "",
                "| Field | Value |", "|---|---|",
                "| ID | %s |" % sid.upper(), "| Name | %s |" % sc["name"], "| Phase 0 report | %s |" % sc.get("pdf", "not in Table 10 (added)"),
                "| Layers involved | %s |" % sc["layers"],
                "| Description | %s |" % sc["description"],
                "| Trigger | `python scenarios\\run_scenario.py %s` |" % sid,
                "| Expected: Twin | %s |" % sc["expect"]["twin"], "| Expected: Guardian | %s |" % sc["expect"]["guardian"],
                "| Expected: Brain | %s |" % sc["expect"]["brain"],
                "| How to verify | Timeline screen; log file `logs/%s-*.log`; the PASS/FAIL lines at its end |" % sid, ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", help="t1 ... t12, or all")
    ap.add_argument("--layers", choices=["mocks", "real"], default=None, help="our stand-ins (default) or the partner layers")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--testbook", action="store_true")
    ap.add_argument("--twin-config", default=os.path.join(ROOT, "twin", "city.yaml"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--log-dir", default=os.path.join(ROOT, "logs"))
    a = ap.parse_args()
    if a.list:
        for sid, sc in defs.SCENARIOS.items():
            print("%-4s %s  [%s]" % (sid, sc["name"], sc["layers"]))
        return 0
    if a.testbook:
        write_testbook(os.path.join(ROOT, "twin", "TESTBOOK.md"))
        print("wrote twin/TESTBOOK.md")
        return 0
    if not a.scenario:
        ap.error("give a scenario id (t1 ... t12), 'all', --list or --testbook")
    ids = list(defs.SCENARIOS) if a.scenario == "all" else [s.strip().lower() for s in a.scenario.split(",")]
    for i in ids:
        if i not in defs.SCENARIOS:
            ap.error("unknown scenario %s; use --list" % i)
    cfg = load_cfg(a)
    results = []
    for i in ids:
        ok, path = run_one(cfg, i, a.log_dir)
        results.append((i, ok, path))
        print()
    print("Summary:")
    for i, ok, path in results:
        print("  %-4s %s  %s" % (i.upper(), "PASS" if ok else "FAIL", os.path.relpath(path, ROOT)))
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
