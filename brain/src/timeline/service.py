"""Timeline service: every layer's messages turned into readable rows (R3), and run logs (R2).

Independent of MQTT and HTTP: `handle(topic, payload, retained)` normalises one message into
zero or more rows, updates the screen state (layer pills, incidents, last decision, city map,
counters) and writes the rows to the run log. The HTTP server (server.py) reads `recent()`,
`snapshot()` and subscribes to new rows.

A row:
    {"seq": 17, "ts": "2026-11-14T10:22:03.120Z", "layer": "TWIN|GUARDIAN|BRAIN|LLM",
     "kind": "reading_alert", "text": "...", "level": "info|ok|warn|error",
     "cid": "<correlation id>", "incident_id": ..., "topic": ..., "signed": true|false|null}

`ts` is the producer's timestamp (as sent), `rx` the time the timeline received it.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from typing import Any, Callable

from ..common import config
from ..common.envelope import EnvelopeError, parse_envelope, verify_hmac
from ..common.log import EventLogger, iso_utc
from .runlog import RunLog, parse_iso

ROW_FIELDS = ("seq", "ts", "rx", "layer", "kind", "text", "level", "cid", "incident_id", "topic", "signed")


iso = iso_utc


def _pattern(template: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\{[a-z_]+\})", template):
        out += f"(?P<{part[1:-1]}>[^/]+)" if part.startswith("{") else re.escape(part)
    return re.compile(f"^{out}$")


def _sub(template: str) -> str:
    return re.sub(r"\{[a-z_]+\}", "+", template)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


class TimelineService:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        logger: EventLogger | None = None,
        timeline_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        security_cfg: dict[str, Any] | None = None,
        hmac_keys: dict[str, bytes] | None = None,
        runlog: RunLog | None = None,
    ):
        self.cfg = timeline_cfg or config.load("timeline")
        self.topics = topics_cfg or config.load("topics")
        self.security = security_cfg or config.load("security")
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-timeline", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.keys = hmac_keys or {}
        self.display = self.cfg.get("display", {})
        logs = self.cfg.get("logs", {})
        self.runlog = runlog or RunLog(config.resolve(logs.get("dir", "../logs")), clock=clock,
                                       live_prefix=logs.get("live_prefix", "live"), fsync=bool(logs.get("fsync")))
        self._routes = self._build_routes()
        self._lock = threading.RLock()
        self._seq = 0
        self._rows: deque[dict[str, Any]] = deque(maxlen=int(self.cfg.get("buffer", {}).get("recent_rows", 2000)))
        self._subscribers: list[queue.Queue] = []
        self._qmax = int(self.cfg.get("sse", {}).get("client_queue", 2000))
        self._suppress = set(self.display.get("suppress_events", []))
        self._levels = {**{e: "error" for e in self.display.get("error_events", [])},
                        **{e: "warn" for e in self.display.get("warn_events", [])},
                        **{e: "ok" for e in self.display.get("ok_events", [])}}
        self._ed25519 = self.security.get("guardian_signature", {}).get("mode") == "ed25519"

        # screen state
        self.layers: dict[str, dict[str, Any]] = {k: {"status": "unknown", "since": None, "reason": None}
                                                  for k in ("twin", "guardian", "brain", "llm")}
        self.safe_mode = False
        self.zones: list[dict[str, Any]] = []
        self.sensors: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}        # actuators and units: zone, type
        self.unit_zone: dict[str, tuple[str | None, str | None]] = {}   # unit -> (zone, status)
        self.quarantined: dict[str, str] = {}               # device -> reason
        self.incidents: dict[str, dict[str, Any]] = {}
        self.decision: dict[str, Any] | None = None
        self.proposals: deque[dict[str, Any]] = deque(maxlen=50)
        self.latest: dict[str, tuple[float, float, str]] = {}   # sensor -> (value, rx time, timestamp)
        self._above: dict[str, bool] = {}
        self._spark: dict[tuple[str, str], deque[float]] = {}  # (zone, type) -> points
        self._spark_last: dict[tuple[str, str], float] = {}
        self._verdicts: dict[str, str] = {}
        self._shown_evidence: dict[str, set[str]] = {}       # incident -> sensors shown (readings)
        self._shown_verdict: dict[str, set[str]] = {}        # incident -> sensors whose verdict was shown
        self._cmd_cid: dict[str, str] = {}                   # Brain command id -> chain correlation id
        self._cmd_text: dict[str, str] = {}
        # city map: what the devices are doing, and what just happened (drawn for map.recent_s)
        self.act_state: dict[str, str] = {}                  # actuator -> last state the Twin reported
        self.outcomes: dict[str, dict[str, Any]] = {}        # target -> last outcome (sent/refused/...)
        self.commands: deque[dict[str, Any]] = deque(maxlen=60)   # recent Brain commands and their acks
        self.calls: deque[dict[str, Any]] = deque(maxlen=60)      # recent emergency calls
        self.cases: dict[str, dict[str, Any]] = {}            # zone -> newest case memory verdict
        self.counters = {"messages": 0, "errors": 0, "sent": 0, "acked": 0, "refused": 0, "failed": 0,
                         "retried": 0, "alerts": 0, "bad_signatures": 0}
        self._rate: deque[float] = deque()
        self._err_shown = (0, 0)

    # --- routing ------------------------------------------------------------------------

    def _build_routes(self) -> list[tuple[str, re.Pattern[str], str]]:
        """(kind, regex, subscription) for every subscribed topic of topics.yaml."""
        t, sub = self.topics, self.cfg.get("subscribe", {})
        routes: list[tuple[str, re.Pattern[str], str]] = []
        for key in sub.get("external_in", []):
            p = t["external_in"][key]["pattern"]
            routes.append((f"in.{key}", _pattern(p), _sub(p)))
        for key in sub.get("external_out", []):
            p = t["external_out"][key]["topic"]
            routes.append((f"out.{key}", _pattern(p), _sub(p)))
        for key in sub.get("control", []):
            p = t["control"][key]["topic"]
            routes.append((f"control.{key}", _pattern(p), _sub(p)))
        for key in sub.get("internal", []):
            p = t["internal"][key]["topic"]
            routes.append((f"internal.{key}", _pattern(p), _sub(p)))
        return routes

    def subscriptions(self) -> list[tuple[str, int]]:
        seen: list[str] = []
        for _, _, s in self._routes:
            if s not in seen:
                seen.append(s)
        return [(s, 0 if "reading" in s else 1) for s in seen]

    def route(self, topic: str) -> tuple[str, dict[str, str]] | None:
        for kind, rx, _ in self._routes:
            m = rx.match(topic)
            if m:
                return kind, m.groupdict()
        return None

    # --- entry points -------------------------------------------------------------------

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> list[dict[str, Any]]:
        """Process one message; returns the rows it produced. Never raises."""
        now = self.clock()
        with self._lock:
            self.counters["messages"] += 1
            self._rate.append(now)
            r = self.route(topic)
            if r is None:
                return []
            kind, vars_ = r
            try:
                body, signed = self._open(kind, payload)
            except (ValueError, UnicodeDecodeError, EnvelopeError) as e:
                self.log.warning("timeline_bad_payload", topic=topic, error=str(e)[:200])
                return []
            if not isinstance(body, dict):
                return []
            try:
                rows = self._dispatch(kind, vars_, topic, body, signed, retained, now)
            except Exception as e:  # a strange message never stops the screen
                self.log.warning("timeline_message_skipped", topic=topic, error=repr(e)[:300])
                return []
            return rows

    def recent(self, limit: int = 300, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._rows if r["seq"] > after]
        return rows[-limit:] if limit else rows

    def chain(self, cid: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self._rows if r.get("cid") == cid]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._qmax)
        with self._lock:
            self._subscribers.append(q)
        return q

    def is_subscribed(self, q: queue.Queue) -> bool:
        with self._lock:
            return q in self._subscribers

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close(self) -> None:
        self.runlog.close()

    # --- envelope -----------------------------------------------------------------------

    def _open(self, kind: str, payload: bytes | str) -> tuple[Any, bool | None]:
        """Body of a message and its signature mark: True ok, False bad / missing, None not checked."""
        doc = json.loads(payload)
        if not (isinstance(doc, dict) and isinstance(doc.get("body"), str) and "v" in doc):
            # internal topics and the run control are plain JSON; an external one should not be
            return doc, (False if kind.startswith(("in.", "out.")) else None)
        env = parse_envelope(payload)
        body = json.loads(env.body)
        if not self.display.get("verify_signatures", True):
            return body, None
        if kind.startswith("in."):
            layer = self.topics["external_in"][kind[3:]].get("source", "twin")
            key = self.keys.get(layer)
            if key is None or (layer == "guardian" and self._ed25519):
                return body, None
            names = [self.security.get("hmacs_self", "brain")]
        else:   # sealed by the Brain itself: check the Twin's (or Guardian's) copy
            key, names = None, []
            for layer in ("twin", "guardian"):
                if layer in self.keys:
                    key, names = self.keys[layer], [layer]
                    break
            if key is None:
                return body, None
        try:
            verify_hmac(env, key, names[0])
            return body, True
        except EnvelopeError:
            self.counters["bad_signatures"] += 1
            return body, False

    # --- rows ---------------------------------------------------------------------------

    def _row(self, layer: str, kind: str, text: str, *, level: str = "info", ts: Any = None,
             cid: str | None = None, incident_id: str | None = None, topic: str | None = None,
             signed: bool | None = None, now: float, body: dict[str, Any] | None = None,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
        row = {"ts": ts if parse_iso(ts) else iso(now), "rx": iso(now), "layer": layer, "kind": kind,
               "text": text, "level": level, "cid": cid, "incident_id": incident_id, "topic": topic,
               "signed": signed}
        if extra:
            row.update(extra)
        if body is not None:
            row["body"] = body
        return row

    def _emit(self, rows: list[dict[str, Any]], show: bool = True) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            self._seq += 1
            row["seq"] = self._seq
            rec = dict(row, shown=show)
            try:
                self.runlog.write(rec)
            except OSError as e:
                self.log.error("run_log_write_failed", error=repr(e))
            if not show:
                continue
            screen = {k: v for k, v in row.items() if k != "body"}
            self._rows.append(screen)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(screen)
                except queue.Full:
                    self._subscribers.remove(q)      # a screen this far behind reconnects
            out.append(screen)
        return out

    # --- per kind -----------------------------------------------------------------------

    def _dispatch(self, kind: str, v: dict[str, str], topic: str, b: dict[str, Any], signed: bool | None,
                  retained: bool, now: float) -> list[dict[str, Any]]:
        ts = b.get("timestamp")
        R = lambda *a, **k: self._row(*a, ts=ts, topic=topic, signed=signed, now=now, **k)   # noqa: E731

        if kind == "internal.reading":
            return self._emit(self._reading(b, now, R))
        if kind == "internal.meta":
            self._meta(b)
            return []
        if kind == "control.run":
            return self._run(b, retained, R)

        if kind == "out.event":
            ev = str(b.get("event", "event"))
            layer = "LLM" if ev.startswith("llm") else "BRAIN"
            self._count_event(ev)
            row = R(layer, ev, str(b.get("summary", ev)), level=self._levels.get(ev, "info"),
                    cid=b.get("correlation_id"), incident_id=b.get("incident_id"), body=b)
            return self._emit([row], show=ev not in self._suppress)
        if kind == "out.error":
            self.counters["errors"] += 1
            text = f"REJECTED {b.get('code', '?')} on {b.get('topic', '?')}: {b.get('detail', '')}"
            row = R("BRAIN", "input_rejected", text, level="error", cid=b.get("message_id"), body=b)
            sec = int(now)
            start, n = self._err_shown
            n = n + 1 if start == sec else 1
            self._err_shown = (sec, n)
            return self._emit([row], show=n <= int(self.display.get("error_rows_per_s", 5)))
        if kind == "out.alert":
            self.counters["alerts"] += 1
            text = f"ALERT {b.get('alert', '?')} {b.get('target_id', '')}: {b.get('detail', '')}"
            return self._emit([R("BRAIN", "alert", text, level="error", cid=b.get("correlation_id"), body=b)])
        if kind == "out.cmd":
            cid = b.get("id")
            if cid:
                self._cmd_cid[cid] = b.get("correlation_id") or cid
            params = b.get("parameters") or {}
            ptxt = (" [" + ", ".join(f"{k}={_fmt(x)}" for k, x in params.items()) + "]") if params else ""
            text = f"command {cid} -> {b.get('target_id', v.get('target_id'))}: {b.get('action')}{ptxt}"
            if cid:
                self._cmd_text[cid] = f"{b.get('action')} {b.get('target_id')}"
            self.commands.append({"id": cid, "target_id": b.get("target_id", v.get("target_id")),
                                  "action": b.get("action"), "at": now, "result": None})
            return self._emit([R("BRAIN", "command", text, cid=self._cmd_cid.get(cid, cid),
                                 extra={"command_id": cid})])
        if kind == "out.status":
            return self._layer("brain", b.get("status"), None, retained, R)

        if kind == "internal.llm_status":
            reason = b.get("reason")
            return self._layer("llm", b.get("status"), reason, retained, R,
                               extra=f" ({b['model']})" if b.get("model") else "")
        if kind == "internal.layer_status":
            return self._layer(b.get("layer") or v.get("layer", "?"), b.get("status"), b.get("reason"), retained, R)
        if kind in ("in.twin_status", "in.guardian_status"):
            return self._layer("twin" if kind == "in.twin_status" else "guardian", b.get("status"), None,
                               retained, R)

        if kind == "internal.incident":
            return self._emit(self._incident(b, now, R))
        if kind == "internal.proposal":
            self.proposals.append(b)
            if b.get("origin") not in self.display.get("proposal_rows", ["rules"]):
                return []
            acts = ", ".join(f"{a.get('action')} {a.get('target_id')}" for a in b.get("actions", []))
            layer = "LLM" if b.get("origin") == "llm" else "BRAIN"
            return self._emit([R(layer, "proposal", f"{b.get('origin', '?')} proposal: {acts or 'no action'}",
                                 cid=b.get("correlation_id"), incident_id=b.get("incident_id"))])
        if kind == "internal.decision":
            return self._emit([self._decision(b, R)])
        if kind == "internal.case":             # map only: the memory's brain/event is the row
            if b.get("zone_id"):
                self.cases[b["zone_id"]] = {"verdict": b.get("verdict"), "share": b.get("share"),
                                            "votes": b.get("votes"), "k": b.get("k"), "at": now}
            return []
        if kind == "internal.outcome":          # map only: brain/event already has the rows
            if b.get("target_id"):
                self.outcomes[b["target_id"]] = {k: b.get(k) for k in ("status", "action", "rule", "incident_id")}
                self.outcomes[b["target_id"]]["at"] = now
            return []

        if kind == "in.call":
            zone = b.get("zone_id", v.get("zone_id"))
            self.calls.append({"zone_id": zone, "category": b.get("category"), "at": now})
            text = f"emergency call ({b.get('category', '?')}) in {self._zone(zone)}"
            if b.get("text"):
                text += f': "{str(b["text"])[:140]}"'
            return self._emit([R("TWIN", "call", text, level="warn", cid=b.get("id"))])
        if kind in ("in.actuator", "in.unit"):
            return self._emit(self._twin_device(kind, b, retained, R))
        if kind == "in.verdict":
            return self._emit(self._verdict(b, retained, R))
        if kind == "in.quarantine":
            dev, state = b.get("device_id", "?"), b.get("state")
            if state == "quarantined":
                self.quarantined[dev] = b.get("reason", "")
            else:
                self.quarantined.pop(dev, None)
            if retained:
                return []
            return self._emit([R("GUARDIAN", f"device_{state}", f"{dev} {state} ({b.get('reason', '')})",
                                 level="error" if state == "quarantined" else "ok", cid=b.get("id"))])
        return []

    def _count_event(self, ev: str) -> None:
        key = {"command_sent": "sent", "command_acknowledged": "acked", "action_refused": "refused",
               "command_failed": "failed", "command_retried": "retried"}.get(ev)
        if key:
            self.counters[key] += 1
        if ev == "safe_mode_on":
            self.safe_mode = True
        elif ev == "safe_mode_off":
            self.safe_mode = False

    def _layer(self, layer: str, status: Any, reason: Any, retained: bool, R, extra: str = "") -> list[dict]:
        status = str(status or "unknown")
        cur = self.layers.setdefault(layer, {"status": "unknown", "since": None, "reason": None})
        changed = cur["status"] != status
        cur.update(status=status, reason=reason)
        if changed:
            cur["since"] = iso(self.clock())
        if layer == "guardian":
            self.safe_mode = status == "offline"
        if not changed:
            return []
        level = {"online": "ok", "offline": "error" if layer != "guardian" else "warn", "degraded": "warn"}.get(status, "info")
        name = {"twin": "TWIN", "guardian": "GUARDIAN", "brain": "BRAIN", "llm": "LLM"}.get(layer, "BRAIN")
        text = f"{layer} {status}{extra}" + (f" ({reason})" if reason else "")
        if layer == "guardian" and status == "offline":
            text += ": SAFE MODE"
        return self._emit([R(name, f"{layer}_{status}", text, level=level)])

    def _run(self, b: dict[str, Any], retained: bool, R) -> list[dict]:
        action = b.get("action")
        run_id, scen = b.get("run_id"), b.get("scenario_id")
        if action == "start":
            if self.runlog.run is not None and self.runlog.run.get("run_id") == run_id:
                return []
            self.runlog.start_run(run_id, scen, b.get("timestamp"))
            self.log.info("run_started", run_id=run_id, scenario_id=scen, file=self.runlog.file_name)
            return self._emit([R("BRAIN", "run_start", f"RUN START scenario {scen} (run {run_id}) "
                                 f"-> logs/{self.runlog.file_name}", level="ok", body=b)])
        if action == "stop":
            if self.runlog.run is None or retained:
                return []
            if run_id and self.runlog.run.get("run_id") not in (None, run_id):
                return []
            rows = self._emit([R("BRAIN", "run_stop", f"RUN STOP scenario {scen or self.runlog.run.get('scenario_id')}"
                                 f" (run {run_id})", level="ok", body=b)])
            self.runlog.stop_run(run_id)
            self.log.info("run_stopped", run_id=run_id)
            return rows
        return []

    def _meta(self, b: dict[str, Any]) -> None:
        self.zones = [{"zone_id": z["zone_id"], "name": z.get("name") or z["zone_id"],
                       "adjacent": list(z.get("adjacent") or [])} for z in b.get("zones", [])]
        self.sensors = {s["sensor_id"]: s for s in b.get("sensors", [])}
        self.devices = {a["actuator_id"]: {"zone_id": a.get("zone_id"), "type": a.get("type"), "kind": "actuator",
                                           "controls": list(a.get("controls") or [])}
                        for a in b.get("actuators", [])}
        for u in b.get("units", []):
            self.devices[u["unit_id"]] = {"zone_id": u.get("home_zone"), "type": u.get("type"), "kind": "unit"}
            self.unit_zone.setdefault(u["unit_id"], (u.get("home_zone"), "free"))

    def _zone(self, zone_id: Any) -> str:
        for z in self.zones:
            if z["zone_id"] == zone_id and z["name"] != zone_id:
                return f"{zone_id} {z['name']}"
        return str(zone_id)

    def _reading(self, b: dict[str, Any], now: float, R) -> list[dict]:
        sid, zone, typ, val = b["sensor_id"], b["zone_id"], b["type"], float(b["value"])
        self.latest[sid] = (val, now, b.get("timestamp") or iso(now))
        key = (zone, typ)
        every = float(self.display.get("reading_sample_s", 1))
        if now - self._spark_last.get(key, float("-inf")) >= every:
            self._spark_last[key] = now
            d = self._spark.setdefault(key, deque(maxlen=int(self.display.get("sparkline_points", 60))))
            d.append(round(val, 3))
        alert = (self.sensors.get(sid) or {}).get("alert")
        if alert is None or not self.display.get("alert_crossing_rows", True):
            return []
        above = val >= float(alert)
        was = self._above.get(sid, False)
        self._above[sid] = above
        if above == was:
            return []
        unit = b.get("unit", "")
        if above:
            return [R("TWIN", "reading_alert", f"{sid} = {_fmt(val)} {unit} above alert {_fmt(alert)} "
                      f"({self._zone(zone)})", level="warn", cid=b.get("correlation_id"))]
        return [R("TWIN", "reading_normal", f"{sid} = {_fmt(val)} {unit} back under alert {_fmt(alert)} "
                  f"({self._zone(zone)})", level="ok", cid=b.get("correlation_id"))]

    def _incident(self, b: dict[str, Any], now: float, R) -> list[dict]:
        iid, status = b.get("incident_id", "?"), b.get("status", "?")
        cid = b.get("correlation_id")
        rows: list[dict] = []
        shown = self._shown_evidence.setdefault(iid, set())
        for ev in b.get("evidence", []):
            if ev.get("kind") != "reading" or ev.get("source_id") in shown or status == "closed":
                continue
            sid = ev["source_id"]
            shown.add(sid)
            unit = ev.get("unit") or (self.sensors.get(sid) or {}).get("unit", "")
            val = ev.get("value")
            ts = self.latest.get(sid, (None, None, None))[2]
            trust = ev.get("trust")
            mark = " (rejected by Guardian)" if trust is not None and float(trust) <= 0 else ""
            rows.append(self._row("TWIN", "evidence", f"reading {sid} = {_fmt(val) if val is not None else '?'} "
                                  f"{unit}{mark} [evidence of {b.get('type')}]", ts=ts, cid=cid, incident_id=iid,
                                  topic="evidence", now=now, level="warn" if mark else "info"))
        conf = b.get("confidence")
        txt = (f"incident {status}: {b.get('type')} in {self._zone(b.get('zone_id'))}, "
               f"confidence {_fmt(float(conf)) if conf is not None else '?'}, severity {b.get('severity')}"
               + ("" if b.get("verified", True) else ", NOT verified (Guardian offline)"))
        if b.get("reason"):
            txt += f" - {b['reason']}"
        level = "ok" if status == "closed" else ("warn" if b.get("severity") in ("high", "critical") else "info")
        rows.append(R("BRAIN", f"incident_{status}", txt, level=level, cid=cid, incident_id=iid))
        entry = {k: b.get(k) for k in ("incident_id", "type", "zone_id", "status", "confidence", "severity",
                                       "verified", "correlation_id", "reason")}
        entry["zone_name"] = self._zone(b.get("zone_id"))
        entry["evidence_n"] = len(b.get("evidence", []))
        entry["updated"] = now
        self.incidents[iid] = entry
        return rows

    def _decision(self, b: dict[str, Any], R) -> dict:
        acts = ", ".join(f"{a.get('action')} {a.get('target_id')}" for a in b.get("actions", [])) or "no action"
        alts = b.get("alternatives") or []
        score = b.get("score")
        vs = "; ".join(f"{a.get('origin', '?')} {_fmt(float(a.get('score', 0)))}" for a in alts)
        text = (f"decision: {acts} ({b.get('origin', '?')} {_fmt(float(score)) if score is not None else '?'}"
                + (f" over {vs}" if vs else "") + ")")
        if b.get("mode") == "safe":
            text += " SAFE MODE"
        if b.get("explanation"):
            text += f" - {str(b['explanation'])[:160]}"
        self.decision = {k: b.get(k) for k in ("id", "incident_id", "origin", "score", "alternatives", "risk",
                                               "mode", "explanation", "timestamp", "correlation_id")}
        self.decision["actions"] = [f"{a.get('action')} {a.get('target_id')}" for a in b.get("actions", [])]
        return R("BRAIN", "decision", text, level="warn" if b.get("mode") == "safe" else "info",
                 cid=b.get("correlation_id"), incident_id=b.get("incident_id"))

    def _twin_device(self, kind: str, b: dict[str, Any], retained: bool, R) -> list[dict]:
        is_unit = kind == "in.unit"
        dev = b.get("unit_id" if is_unit else "actuator_id", "?")
        if is_unit and (b.get("zone_id") or b.get("status")):
            prev = self.unit_zone.get(dev, (None, None))
            self.unit_zone[dev] = (b.get("zone_id") or prev[0], b.get("status") or prev[1])
        if not is_unit and b.get("state"):
            self.act_state[dev] = str(b["state"])
        if retained:
            return []
        result = b.get("result")
        if result:
            cmd = b.get("correlation_id")
            for c in reversed(self.commands):
                if c["id"] == cmd:
                    c["result"] = result
                    break
            what = self._cmd_text.get(cmd, "")
            detail = b.get("state") or b.get("status") or ""
            text = f"ack {dev}: {result}" + (f" for {cmd}" if cmd else "") + (f" ({what})" if what else "")
            if detail:
                text += f", state {detail}"
            if b.get("reason"):
                text += f", reason {b['reason']}"
            return [R("TWIN", "ack", text, level="ok" if result == "done" else "error",
                      cid=self._cmd_cid.get(cmd, cmd), extra={"command_id": cmd})]
        if is_unit and b.get("status"):
            zone = b.get("zone_id")
            return [R("TWIN", "unit_status", f"{dev} {b['status']}" + (f" in {self._zone(zone)}" if zone else ""),
                      cid=self._cmd_cid.get(b.get("correlation_id"), b.get("correlation_id")))]
        if b.get("state"):
            return [R("TWIN", "actuator_state", f"{dev} state {b['state']}", cid=b.get("correlation_id"))]
        return []

    def _verdict(self, b: dict[str, Any], retained: bool, R) -> list[dict]:
        sid, verdict = b.get("sensor_id", "?"), b.get("verdict", "?")
        prev = self._verdicts.get(sid)
        self._verdicts[sid] = verdict
        if retained:
            return []
        mode = self.display.get("verdict_rows", "changes")
        evidence_of = [iid for iid, inc in self.incidents.items()
                       if inc.get("status") != "closed" and sid in self._shown_evidence.get(iid, set())
                       and sid not in self._shown_verdict.get(iid, set())]
        show = (mode == "all" or verdict != "trusted" or (prev is not None and prev != verdict) or bool(evidence_of))
        if not show:
            return []
        for iid in evidence_of:
            self._shown_verdict.setdefault(iid, set()).add(sid)
        score = b.get("trust_score")
        text = f"{sid}: {verdict}" + (f" (trust {_fmt(float(score))})" if score is not None else "")
        if b.get("reason") and verdict != "trusted":
            text += f", {b['reason']}"
        level = {"trusted": "ok", "suspicious": "warn", "rejected": "error"}.get(verdict, "info")
        return [R("GUARDIAN", f"verdict_{verdict}", text, level=level, cid=b.get("correlation_id") or b.get("id"),
                  incident_id=evidence_of[0] if evidence_of else None)]

    # --- snapshot -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the header and the side panel show."""
        now = self.clock()
        with self._lock:
            while self._rate and now - self._rate[0] > 5:
                self._rate.popleft()
            keep = float(self.display.get("closed_incident_keep_s", 20))
            for iid, inc in list(self.incidents.items()):
                if inc.get("status") == "closed" and now - inc["updated"] > keep:
                    del self.incidents[iid]
                    self._shown_evidence.pop(iid, None)
                    self._shown_verdict.pop(iid, None)
            open_by_zone: dict[str, dict] = {}
            for inc in self.incidents.values():
                if inc.get("status") != "closed":
                    open_by_zone.setdefault(inc.get("zone_id"), inc)
            zones = []
            for z in self.zones:
                zid = z["zone_id"]
                q = [d for d in self.quarantined
                     if (self.devices.get(d) or self.sensors.get(d) or {}).get("zone_id") == zid]
                units = [u for u, (uz, st) in self.unit_zone.items() if uz == zid]
                latest: dict[str, Any] = {}
                worst = None
                for sid, s in self.sensors.items():
                    if s.get("zone_id") != zid or sid not in self.latest:
                        continue
                    val = self.latest[sid][0]
                    latest[s["type"]] = {"value": round(val, 3), "unit": s.get("unit")}
                    if s.get("alert"):
                        ratio = val / float(s["alert"]) if float(s["alert"]) else 0
                        if worst is None or ratio > worst[0]:
                            worst = (ratio, s["type"], s.get("unit"), float(s["alert"]))
                spark = None
                if worst is not None:
                    spark = {"type": worst[1], "unit": worst[2], "alert": worst[3], "ratio": round(worst[0], 3),
                             "points": list(self._spark.get((zid, worst[1]), []))}
                inc = open_by_zone.get(zid)
                state = "incident" if inc else ("quarantine" if q else "normal")
                zones.append({**z, "state": state, "quarantined": q, "units": units, "latest": latest,
                              "spark": spark, "incident": ({"type": inc["type"], "severity": inc["severity"]}
                                                           if inc else None)})
            return {
                "now": iso(now),
                "layers": self.layers,
                "safe_mode": self.safe_mode,
                "run": self.runlog.run,
                "log_file": self.runlog.file_name,
                "counters": {**self.counters, "msgs_per_s": round(len(self._rate) / 5, 1)},
                "incidents": sorted(self.incidents.values(), key=lambda i: -i["updated"]),
                "decision": self.decision,
                "zones": zones,
                "quarantined": dict(self.quarantined),
                "units": {u: {"zone_id": z, "status": s, "type": (self.devices.get(u) or {}).get("type"),
                              "home_zone": (self.devices.get(u) or {}).get("zone_id")}
                          for u, (z, s) in self.unit_zone.items()},
                "map": self._map_state(now),
            }

    def _map_state(self, now: float) -> dict[str, Any]:
        """What the city map draws beyond the zones: devices with their state, sensors with Guardian's
        verdict, and what happened in the last map.recent_s seconds (commands, refusals, calls)."""
        m = self.cfg.get("map", {})
        recent = float(m.get("recent_s", 12))
        actuators = {}
        for d, info in self.devices.items():
            if info.get("kind") != "actuator":
                continue
            out = self.outcomes.get(d)
            actuators[d] = {"zone_id": info.get("zone_id"), "type": info.get("type"),
                            "controls": info.get("controls", []), "state": self.act_state.get(d),
                            "outcome": ({**out, "age_s": round(now - out["at"], 1)}
                                        if out and now - out["at"] <= recent else None)}
        return {
            "layout": m.get("layout") or {},
            "sea": m.get("sea"),
            "call_s": float(m.get("call_s", 30)),
            "actuators": actuators,
            "sensors": {sid: {"zone_id": s.get("zone_id"), "type": s.get("type"), "alert": s.get("alert"),
                              "verdict": self._verdicts.get(sid)} for sid, s in self.sensors.items()},
            "commands": [{**{k: c[k] for k in ("id", "target_id", "action", "result")},
                          "age_s": round(now - c["at"], 1)} for c in self.commands if now - c["at"] <= recent],
            "cases": {z: {k: c[k] for k in ("verdict", "share", "votes", "k")}
                      for z, c in self.cases.items()
                      if now - c["at"] <= float(m.get("case_s", 20)) and c.get("verdict") != "quiet"},
            "calls": [{"zone_id": c["zone_id"], "category": c["category"], "age_s": round(now - c["at"], 1)}
                      for c in self.calls if now - c["at"] <= float(m.get("call_s", 30))],
        }
