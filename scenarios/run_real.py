"""Scenario runner for the REAL layers: the real Twin, the real Guardian and the Brain on the triplet broker.

    python scenarios/run_real.py R2          run one scenario
    python scenarios/run_real.py R1 R3 R2    several, in that order
    python scenarios/run_real.py --all       every scenario in scenarios/real/ (about 12 minutes)
    python scenarios/run_real.py --list      list them, with the Test Book scenario each one stands for
    python scenarios/run_real.py --check     validate the files against the city the Brain knows

The Test Book runner (run.py) drives the Brain's mock Twin and mock Guardian through their control topics. The
real Twin does not implement those (integration/MATCHING.md Q1), so this runner triggers each situation through
the real Twin's REST API instead (rain, smog, power demand, faults, emergency calls), and can stop and restart
the real Guardian or Twin for the "layer down" scenarios. What it checks is unchanged: the same `expect` entries
as the Test Book (scenariolib), over the messages it observes on the broker with the `tester` account.

Needs: the broker, the Brain WITHOUT its Twin/Guardian mocks (`python run.py`, or `--mock-llm`), the real
Guardian (Brain profile) and the real Twin (`--fake`, integration on); see triplet-05-run.md.

Before every scenario: Twin reset (calm, faults cleared, valves closed, units home), Guardian reset (quarantines
released), `settle_s` of quiet. After it: the same reset, and any layer the scenario stopped is started again.
Each run writes logs/<ID>-<YYYYMMDD-HHMMSS>.log (JSON lines, same format as run.py). Exit code 0 = all PASS,
1 = a FAIL, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as base  # noqa: E402  (also puts brain/ on sys.path)
import scenariolib as lib  # noqa: E402

from src.common.envelope import seal  # noqa: E402
from src.common.log import utc_now_iso  # noqa: E402

REAL_DIR = lib.SCENARIO_DIR / "real"
LOG_DIR = lib.REPO_ROOT / "logs"
SERVICE_LOG_DIR = LOG_DIR / "services"
SETTLE_SUBSCRIBE_S = 1.5    # retained messages (meta, statuses) arrive right after subscribing
TAIL_S = 2.0
STEP_KEYS = ("at_s", "twin", "body", "stop", "start", "note")
SCENARIO_KEYS = ("id", "name", "testbook", "description", "duration_s", "settle_s", "observe", "steps", "expect")


# --- configuration ------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    with open(REAL_DIR / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["twin_url"] = (os.environ.get("TWIN_URL") or cfg.get("twin_url") or "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("TWIN_TEST_KEY") or cfg.get("test_key")
    if not key:
        with open(lib.REPO_ROOT / "twin" / "city.yaml", encoding="utf-8") as f:
            key = ((yaml.safe_load(f) or {}).get("api") or {}).get("test_key")
    if not key:
        raise SystemExit("no Twin test key: set TWIN_TEST_KEY or test_key in scenarios/real/config.yaml")
    cfg["test_key"] = key
    return cfg


def list_real() -> list[str]:
    def key(i: str) -> tuple:
        digits = "".join(c for c in i if c.isdigit())
        return (int(digits) if digits else 0, i)
    return sorted((p.stem for p in REAL_DIR.glob("R*.yaml")), key=key)


def load_real(scenario_id: str) -> dict[str, Any]:
    path = REAL_DIR / f"{scenario_id}.yaml"
    if not path.is_file():
        raise SystemExit(f"no scenario file {path.relative_to(lib.REPO_ROOT)} (see --list)")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate(sc: dict[str, Any], city: lib.CityIndex | None, processes: dict[str, Any]) -> list[str]:
    p = [f"unknown key {k!r}" for k in sc if k not in SCENARIO_KEYS]
    for k in ("id", "name", "duration_s", "expect"):
        if k not in sc:
            p.append(f"missing {k}")
    dur = float(sc.get("duration_s") or 0)
    if not 0 < dur <= lib.MAX_DURATION_S:
        p.append(f"duration_s must be in (0, {lib.MAX_DURATION_S}]")
    for i, st in enumerate(sc.get("steps") or []):
        where = f"steps[{i}]"
        if not isinstance(st, dict) or not 0 <= float(st.get("at_s", -1)) <= dur:
            p.append(f"{where}: needs at_s within the duration")
            continue
        p += [f"{where}: unknown key {k!r}" for k in st if k not in STEP_KEYS]
        ops = [k for k in ("twin", "stop", "start") if k in st]
        if len(ops) != 1:
            p.append(f"{where}: needs exactly one of twin / stop / start")
        elif ops[0] == "twin" and not str(st["twin"]).split(" ", 1)[0] in ("GET", "POST", "DELETE"):
            p.append(f"{where}: twin must be 'POST /path' (GET, POST or DELETE)")
        elif ops[0] in ("stop", "start") and st[ops[0]] not in processes:
            p.append(f"{where}: unknown layer {st[ops[0]]!r} (config.yaml processes: {sorted(processes)})")
    if city is not None:
        for i, e in enumerate(sc.get("expect") or []):
            p += [f"expect[{i}]: {x}" for x in lib._validate_entry(e, city, dur)]
    return [f"{sc.get('id', '?')}: {x}" for x in p]


# --- the real layers ------------------------------------------------------------------


class Layers:
    """REST calls to the real Twin and stop/start of the real processes."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.http = httpx.Client(base_url=cfg["twin_url"], timeout=10,
                                 headers={"X-API-Key": cfg["test_key"]})
        self.started: dict[str, subprocess.Popen] = {}

    def twin(self, call: str, body: Any = None) -> tuple[int, Any]:
        method, path = call.split(" ", 1)
        r = self.http.request(method, path, json=body)
        try:
            data = r.json()
        except ValueError:
            data = r.text[:300]
        return r.status_code, data

    def twin_healthy(self) -> bool:
        try:
            return self.http.get("/health", timeout=3).json().get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False

    def _pids_on(self, port: int) -> list[int]:
        pids = set()
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                pids.add(c.pid)
        return sorted(pids)

    def stop(self, name: str) -> str:
        port = int(self.cfg["processes"][name]["port"])
        pids = self._pids_on(port)
        if not pids:
            return f"nothing listens on port {port}"
        procs = [psutil.Process(p) for p in pids]
        for pr in procs:
            pr.send_signal(signal.SIGINT)        # a clean stop, like Ctrl+C in its terminal
        _, alive = psutil.wait_procs(procs, timeout=8)
        for pr in alive:
            pr.kill()
        return f"stopped pid {', '.join(map(str, pids))} (port {port})"

    def start(self, name: str) -> str:
        spec = self.cfg["processes"][name]
        if self._pids_on(int(spec["port"])):
            return f"already running on port {spec['port']}"
        subst = {"repo": str(lib.REPO_ROOT), "python": sys.executable, "secrets": str(lib.BRAIN_DIR / "secrets")}
        fill = lambda s: str(s).format(**subst)  # noqa: E731
        env = {**os.environ, **{k: fill(v) for k, v in (spec.get("env") or {}).items()}}
        SERVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(SERVICE_LOG_DIR / f"{name}.log", "a", encoding="utf-8")
        log.write(f"\n--- started by scenarios/run_real.py at {utc_now_iso()} ---\n")
        log.flush()
        proc = subprocess.Popen([fill(a) for a in spec["start"]], cwd=lib.REPO_ROOT / spec.get("cwd", "."), env=env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.started[name] = proc
        return f"started pid {proc.pid}, log {(SERVICE_LOG_DIR / f'{name}.log').relative_to(lib.REPO_ROOT)}"


def status_of(messages: list[dict[str, Any]], layer: str, since_t: float = -1.0) -> str | None:
    for m in reversed(messages):
        if m["topic"] == f"{layer}/status" and m["t"] >= since_t and isinstance(m.get("body"), dict):
            return m["body"].get("status")
    return None


# --- one scenario ---------------------------------------------------------------------


def run_one(sc: dict[str, Any], client, layers: Layers, keys: dict[str, bytes], topics: dict[str, Any],
            cfg: dict[str, Any]) -> bool:
    scenario_id = sc["id"]
    run_topic = topics["control"]["run"]
    rec = base.Recorder()
    client.on_message = rec
    for t in (*lib.OBSERVED_TOPICS, "brain/internal/meta"):
        client.subscribe(t, 1)
    time.sleep(SETTLE_SUBSCRIBE_S)
    if not rec.meta:
        raise SystemExit("no city description on brain/internal/meta: is the Brain running?")
    city = lib.city_from_meta(rec.meta)
    problems = validate(sc, city, cfg["processes"])
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(2)
    for t in lib.reading_subscriptions(sc, city):
        client.subscribe(t, 1)

    sent: list[dict[str, Any]] = []

    def note(kind: str, what: str, result: Any) -> None:
        sent.append({"t": round(time.monotonic() - rec.t0, 3), "ts": utc_now_iso(), "topic": kind,
                     "body": {"call": what, "result": result}})

    def reset(label: str) -> None:
        code, data = layers.twin("POST /scenario/reset")
        note("twin-rest", f"{label}: POST /scenario/reset", {"status": code})
        body = {"id": f"runner-reset-{time.time_ns()}", "timestamp": utc_now_iso(), "source": "tester", "op": "reset"}
        client.publish("guardian/control", seal(body, keys["guardian"]), 1, False)
        note("guardian/control", f"{label}: Guardian reset (quarantines released)", "sent")

    def wait_online(layer: str, since_t: float) -> bool:
        end = time.monotonic() + float(cfg.get("online_timeout_s", 45))
        while time.monotonic() < end:
            if status_of(rec.snapshot(), layer, since_t) == "online":
                return True
            time.sleep(0.5)
        return False

    print(f"== {scenario_id}: {sc['name']} ({sc['duration_s']} s)" +
          (f"  [Test Book {sc['testbook']}]" if sc.get("testbook") else ""))
    for layer in ("twin", "guardian"):
        if status_of(rec.snapshot(), layer) != "online":
            print(f"  [SKIP] {layer} is not online (retained {layer}/status): start it first (triplet-05-run.md)")
            return False
    if not layers.twin_healthy():
        print(f"  [SKIP] the Twin's API does not answer at {cfg['twin_url']}/health")
        return False

    reset("before")
    time.sleep(float(sc.get("settle_s", cfg.get("settle_s", 12))))

    started = datetime.now(timezone.utc)
    run_id = f"run-{scenario_id}-{started:%Y%m%d-%H%M%S}"

    def run_msg(action: str, **extra: Any) -> None:
        body = {"run_id": run_id, "scenario_id": scenario_id, "action": action, "ts": utc_now_iso(), **extra}
        client.publish(run_topic["topic"], json.dumps(body), int(run_topic["qos"]), bool(run_topic["retain"]))
        sent.append({"t": round(time.monotonic() - rec.t0, 3), "ts": body["ts"], "topic": run_topic["topic"], "body": body})

    rec.t0 = time.monotonic()
    rec.messages = [m for m in rec.messages if m["retained"]]
    for m in rec.messages:
        m["t"] = 0.0
    sent.clear()
    run_msg("start")
    stopped: list[str] = []
    for st in sorted(sc.get("steps") or [], key=lambda s: float(s["at_s"])):
        delay = float(st["at_s"]) - (time.monotonic() - rec.t0)
        if delay > 0:
            time.sleep(delay)
        if "twin" in st:
            try:
                code, data = layers.twin(st["twin"], st.get("body"))
                result: Any = {"status": code, "reply": data if code >= 300 else None}
                if code >= 300:
                    print(f"  ! {st['twin']} answered {code}: {str(data)[:200]}")
            except httpx.HTTPError as e:
                result = {"error": repr(e)[:200]}
                print(f"  ! {st['twin']} failed: {e!r}"[:240])
            note("twin-rest", f"{st['twin']} {json.dumps(st.get('body')) if st.get('body') is not None else ''}".strip(), result)
        elif "stop" in st:
            msg = layers.stop(st["stop"])
            stopped.append(st["stop"])
            note("process", f"stop {st['stop']}", msg)
        elif "start" in st:
            msg = layers.start(st["start"])
            if st["start"] in stopped:
                stopped.remove(st["start"])
            note("process", f"start {st['start']}", msg)
    remaining = float(sc["duration_s"]) + TAIL_S - (time.monotonic() - rec.t0)
    if remaining > 0:
        time.sleep(remaining)

    observed = rec.snapshot()
    results = lib.evaluate(sc, observed, city)
    passed = lib.overall(results)
    run_msg("stop", result="PASS" if passed else "FAIL")

    # back to a known state for the next scenario: restart what is down, then reset
    for layer in ("guardian", "twin"):
        down = layer in stopped or status_of(rec.snapshot(), layer) == "offline"
        if down:
            since = time.monotonic() - rec.t0
            print(f"  restarting {layer}: {layers.start(layer)}")
            if not wait_online(layer, since):
                print(f"  ! {layer} did not come back online within {cfg.get('online_timeout_s', 45)} s: "
                      f"see logs/services/{layer}.log")
    if layers.twin_healthy():
        reset("after")

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{scenario_id}-{started:%Y%m%d-%H%M%S}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        def line(obj: dict[str, Any]) -> None:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        line({"kind": "run", "run_id": run_id, "scenario_id": scenario_id, "name": sc["name"],
              "testbook": sc.get("testbook"), "layers": "real",
              "started": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
              "duration_s": sc["duration_s"], "file": f"scenarios/real/{scenario_id}.yaml"})
        for m in sorted([*({"kind": "sent", **s} for s in sent), *({"kind": "observed", **m} for m in observed)],
                        key=lambda x: x["t"]):
            line(m)
        line({"kind": "summary", "run_id": run_id, "scenario_id": scenario_id, "result": "PASS" if passed else "FAIL",
              "observed": len(observed), "expectations": [r.as_dict() for r in results]})
    print("\n".join(lib.report_lines(sc, results)))
    print(f"log: {log_path.relative_to(lib.REPO_ROOT).as_posix()}")
    return passed


# --- main -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run scenarios against the real Twin, the real Guardian and the Brain.")
    ap.add_argument("scenario", nargs="*", help="scenario id(s), e.g. R2")
    ap.add_argument("--all", action="store_true", help="run every scenario in scenarios/real/")
    ap.add_argument("--list", action="store_true", help="list the scenarios")
    ap.add_argument("--check", action="store_true", help="validate the files against the city the Brain knows")
    args = ap.parse_args(argv)

    ids = list_real() if (args.all or args.list or (args.check and not args.scenario)) else args.scenario
    if args.list:
        for i in ids:
            sc = load_real(i)
            tb = f"[{sc['testbook']}]" if sc.get("testbook") else ""
            print(f"{i:<4} {tb:<9} {sc.get('name', '')} ({sc.get('duration_s')} s)")
        return 0
    if not ids:
        ap.print_help()
        return 2

    cfg = load_config()
    scenarios = [load_real(i) for i in ids]
    client = base.connect()
    try:
        if args.check:
            rec = base.Recorder()
            client.on_message = rec
            client.subscribe("brain/internal/meta", 1)
            time.sleep(SETTLE_SUBSCRIBE_S)
            city = lib.city_from_meta(rec.meta) if rec.meta else None
            if city is None:
                print("no city description on brain/internal/meta: only the structure is checked", file=sys.stderr)
            problems = [p for sc in scenarios for p in validate(sc, city, cfg["processes"])]
            print("\n".join(problems) or f"{len(scenarios)} scenario file(s) OK")
            return 1 if problems else 0
        keys = base._keys()
        topics = base.config.load("topics")
        layers = Layers(cfg)
        results: dict[str, bool] = {}
        for sc in scenarios:
            results[sc["id"]] = run_one(sc, client, layers, keys, topics, cfg)
            print()
        if len(results) > 1:
            print("Summary: " + "  ".join(f"{k} {'PASS' if v else 'FAIL'}" for k, v in results.items()))
        return 0 if all(results.values()) else 1
    finally:
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
