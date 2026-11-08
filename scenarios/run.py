"""Scenario runner (R2: one trigger, one log file per run).

    python scenarios/run.py B3          run one scenario
    python scenarios/run.py --all       run every scenario in turn
    python scenarios/run.py --list      list the scenarios
    python scenarios/run.py --check     validate every scenario file (no broker)
    python scenarios/run.py B3 --plan   print the control messages a run would send (no broker)

Needs the broker, the Brain and the mocks running (Twin `python -m mocks.twin`, Guardian
`python -m mocks.guardian`, mock LLM `python -m mocks.mock_llm`). Uses the Brain's broker
settings and keys (brain/config, or BRAIN_CONFIG_DIR). Publishes brain/control/run start /
stop (retained) so the timeline starts a new log, drives the mocks through their control
topics, records what it sees, checks the scenario's `expect` list and writes
logs/<ID>-<YYYYMMDD-HHMMSS>.log (JSON lines). Exit code 0 = PASS, 1 = FAIL, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenariolib as lib  # noqa: E402  (also puts brain/ on sys.path)

from src.common import config  # noqa: E402
from src.common.envelope import seal  # noqa: E402
from src.common.log import utc_now_iso  # noqa: E402

LOG_DIR = lib.REPO_ROOT / "logs"
CONNECT_TIMEOUT_S = 10
SETTLE_S = 1.0          # retained messages arrive right after subscribing
TAIL_S = 2.0            # last messages after the duration


def _keys() -> dict[str, bytes]:
    keys = {}
    for layer, path in config.load("security")["hmac_keys"].items():
        text = config.read_secret(path)
        if not text:
            raise SystemExit(f"missing HMAC key {layer}-brain: {config.resolve(path)} (see brain/README.md)")
        keys[layer] = text.encode("utf-8")
    return keys


class Recorder:
    """Every observed message, with its time since the start of the run."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.messages: list[dict[str, Any]] = []
        self.meta: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def __call__(self, topic: str, payload: bytes, retained: bool) -> None:
        body = lib.decode(payload)
        if topic == "brain/internal/meta":
            self.meta = body
            return
        msg = {"t": round(time.monotonic() - self.t0, 3), "ts": utc_now_iso(), "topic": topic,
               "retained": retained, "body": body}
        if body is None:
            msg["raw"] = payload.decode("utf-8", "replace")[:500] if isinstance(payload, bytes) else str(payload)[:500]
        with self._lock:
            self.messages.append(msg)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.messages)


def run_one(scenario_id: str, client, keys: dict[str, bytes], topics: dict[str, Any]) -> bool | None:
    """True = PASS, False = FAIL, None = skipped (needs the mock LLM)."""
    sc = lib.load_scenario(scenario_id)
    city = lib.city_index()
    problems = lib.validate(sc, city)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(2)
    control = {"twin": "twin/control", "guardian": "guardian/control",
               "llm": topics["control"]["llm"]["topic"]}
    run_topic = topics["control"]["run"]

    rec = Recorder()
    client.on_message = rec
    for t in (*lib.OBSERVED_TOPICS, "brain/internal/meta", *lib.reading_subscriptions(sc, city)):
        client.subscribe(t, 1)
    time.sleep(SETTLE_S)
    if rec.meta:
        city = lib.city_from_meta(rec.meta)     # the city the Brain actually knows

    # LLM modes (offline, slow, invalid) are obeyed by the mock LLM only. With the real LLM they would
    # have no effect and the run would prove nothing, so it is skipped with the command to use.
    llm_modes = {s.body.get("mode") for s in lib.plan(sc) if s.channel == "llm"} - {"scripted"}
    model = next((m["body"].get("model") for m in reversed(rec.messages)
                  if m["topic"] == "brain/internal/llm-status" and isinstance(m.get("body"), dict)), None)
    if llm_modes and model and not str(model).startswith("mock:"):
        print(f"== {scenario_id}: {sc['name']}\n  [SKIP] needs the mock LLM (modes {sorted(llm_modes)}), the "
              f"running LLM is {model}: start the Brain with `python run.py --mocks --mock-llm`, or stop Ollama "
              f"by hand for the live drill")
        return None

    started = datetime.now(timezone.utc)
    run_id = f"run-{scenario_id}-{started:%Y%m%d-%H%M%S}"
    sent: list[dict[str, Any]] = []

    def send(channel: str, body: dict[str, Any]) -> None:
        topic = control[channel]
        payload = seal(body, keys[channel]) if channel in keys else json.dumps(body)
        client.publish(topic, payload, 1, False)
        sent.append({"t": round(time.monotonic() - rec.t0, 3), "ts": utc_now_iso(), "topic": topic, "body": body})

    def run_msg(action: str, **extra: Any) -> None:
        body = {"run_id": run_id, "scenario_id": scenario_id, "action": action, "ts": utc_now_iso(), **extra}
        client.publish(run_topic["topic"], json.dumps(body), int(run_topic["qos"]), bool(run_topic["retain"]))
        sent.append({"t": round(time.monotonic() - rec.t0, 3), "ts": body["ts"], "topic": run_topic["topic"], "body": body})

    print(f"== {scenario_id}: {sc['name']} ({sc['duration_s']} s)")
    rec.t0 = time.monotonic()
    rec.messages = [m for m in rec.messages if m["retained"]]   # keep the retained state for the log
    for m in rec.messages:
        m["t"] = 0.0
    run_msg("start")
    for step in lib.plan(sc):
        delay = step.at_s - (time.monotonic() - rec.t0)
        if delay > 0:
            time.sleep(delay)
        send(step.channel, step.body)
    remaining = float(sc["duration_s"]) + TAIL_S - (time.monotonic() - rec.t0)
    if remaining > 0:
        time.sleep(remaining)

    observed = rec.snapshot()
    results = lib.evaluate(sc, observed, city)
    passed = lib.overall(results)
    run_msg("stop", result="PASS" if passed else "FAIL")
    if sc.get("cleanup", True):
        for step in lib.cleanup_steps():
            send(step.channel, step.body)

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{scenario_id}-{started:%Y%m%d-%H%M%S}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        def line(obj: dict[str, Any]) -> None:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        line({"kind": "run", "run_id": run_id, "scenario_id": scenario_id, "name": sc["name"],
              "started": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
              "duration_s": sc["duration_s"], "file": f"scenarios/{scenario_id}.yaml"})
        for m in sorted([*({"kind": "sent", **s} for s in sent), *({"kind": "observed", **m} for m in observed)],
                        key=lambda x: x["t"]):
            line(m)
        line({"kind": "summary", "run_id": run_id, "scenario_id": scenario_id, "result": "PASS" if passed else "FAIL",
              "observed": len(observed), "expectations": [r.as_dict() for r in results]})
    print("\n".join(lib.report_lines(sc, results)))
    shown = log_path.relative_to(lib.REPO_ROOT) if log_path.is_relative_to(lib.REPO_ROOT) else log_path
    print(f"log: {shown.as_posix()}")
    return passed


def connect():
    from src.common.mqtt import BrainMqtt

    class RunnerMqtt(BrainMqtt):
        """Same broker and TLS settings as the Brain, but the `tester` account: the broker ACL
        only lets it write the mocks' control topics (twin/control, guardian/control,
        brain/control/#), and it can read everything it has to observe."""

        def _configure_security(self, c) -> None:
            super()._configure_security(c)
            auth = config.load("runner").get("auth", {})
            if auth.get("username"):
                c.username_pw_set(auth["username"], config.read_secret(auth.get("password_file")))

    broker = config.load("broker")
    client = RunnerMqtt(broker["client_ids"]["default_prefix"] + "scenario-runner")
    client.start()
    if not client.connected.wait(CONNECT_TIMEOUT_S):
        client.stop()
        raise SystemExit(f"cannot connect to the broker at {broker['host']}:{broker['port']} "
                         f"within {CONNECT_TIMEOUT_S} s (see brain/README.md)")
    return client


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run Test Book scenarios against the Brain and the mocks.")
    ap.add_argument("scenario", nargs="*", help="scenario id(s), e.g. B3")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--list", action="store_true", help="list the scenarios")
    ap.add_argument("--check", action="store_true", help="validate the scenario files, no broker")
    ap.add_argument("--plan", action="store_true", help="print the control messages of a run, no broker")
    args = ap.parse_args(argv)

    ids = lib.list_scenarios() if (args.all or args.check and not args.scenario) else args.scenario
    if args.list:
        for sid in lib.list_scenarios():
            sc = lib.load_scenario(sid)
            print(f"{sid:5} {sc.get('duration_s', '?'):>3} s  {sc.get('name')}")
        return 0
    if not ids:
        ap.print_help()
        return 2
    unknown = [i for i in ids if not lib.scenario_path(i).is_file()]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)} (see --list)", file=sys.stderr)
        return 2
    if args.check:
        city = lib.city_index()
        problems = [p for i in ids for p in lib.validate(lib.load_scenario(i), city)]
        print("\n".join(problems) if problems else f"{len(ids)} scenario file(s) valid")
        return 1 if problems else 0
    if args.plan:
        for i in ids:
            for step in lib.plan(lib.load_scenario(i)):
                print(f"{step.at_s:6.1f} s  {step.channel:8} {json.dumps(step.body)}")
        return 0

    keys, topics = _keys(), config.load("topics")
    client = connect()
    results: dict[str, bool | None] = {}
    try:
        for i in ids:
            results[i] = run_one(i, client, keys, topics)
    finally:
        time.sleep(0.5)          # let the last QoS 1 messages leave
        client.stop()
    if len(results) > 1:
        print("\n== Summary")
        for i, ok in results.items():
            print(f"  {i:5} {'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}")
    return 0 if all(ok is not False for ok in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
