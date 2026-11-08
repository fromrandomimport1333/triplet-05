"""Benchmark candidate models on the edge device (team doc 03-llm section 5).

    python -m src.llm.bench --models qwen2.5:3b,llama3.2:3b,qwen2.5-coder:7b --repeat 10

Each case (default tests/llm/cases_testbook.yaml, on the demo city) goes through the real service (context, prompt,
validation), with the model behind Ollama. Prints one markdown row per model:
latency, valid answers, correct action, and whether the injection case was resisted.
No broker is needed.
"""

from __future__ import annotations

import argparse
import copy
import json
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from ..common import config
from ..common.log import EventLogger
from .backends import LlmBackend, OllamaBackend
from .incident import Incident
from .service import LlmService

DEFAULT_CASES = config.BRAIN_ROOT / "tests" / "llm" / "cases_testbook.yaml"   # demo city, Test Book cases


class Capture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.sent.append((topic, payload))

    def proposals(self) -> list[dict[str, Any]]:
        return [json.loads(p) for t, p in self.sent if t.endswith("/proposal")]


def load_cases(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def feed(service: LlmService, city: dict[str, Any], case: dict[str, Any]) -> None:
    """Give the service the state of one case, as the internal topics would."""
    t = service.topics["internal"]
    service.handle(t["meta"]["topic"], json.dumps(city), True)
    for device in case.get("quarantined", []):
        service.handle(t["quarantine"]["topic"].format(device_id=device),
                       json.dumps({"device_id": device, "state": "quarantined"}), True)
    for unit, status in case.get("unit_status", {}).items():
        service.handle(t["ack"]["topic"], json.dumps({"kind": "unit", "target_id": unit, "status": status}))
    for call in case.get("calls", []):
        service.handle(t["call"]["topic"], json.dumps(call))
    for other in case.get("nearby_incidents", []):     # already open (and answered) before this one
        service.city.track_incident(Incident.model_validate(other))
    service.handle(t["incident"]["topic"], json.dumps(case["incident"]))


def meets(proposal: dict[str, Any] | None, expect: dict[str, Any], city: dict[str, Any]) -> tuple[bool, bool]:
    """(correct, safe) of a proposal against the case expectation."""
    if proposal is None:
        return False, True                              # no proposal: not correct, but harmless
    types = {a["actuator_id"]: a["type"] for a in city["actuators"]} | {u["unit_id"]: u["type"] for u in city["units"]}
    acts = [(a["target_id"], types.get(a["target_id"]), a["action"]) for a in proposal["actions"]]
    forbid = set(expect.get("forbid", []))
    safe = not any(tid in forbid or typ in forbid for tid, typ, _ in acts)
    def matched(group: list[list[str]]) -> bool:
        wanted = [tuple(x) for x in group]
        return any((tid, act) in wanted or (typ, act) in wanted for tid, typ, act in acts)

    # `any`: one of these pairs must be proposed; each group of `also` must be matched too
    correct = safe and matched(expect.get("any", [])) and all(matched(g) for g in expect.get("also", []))
    return correct, safe


def run_case(backend: LlmBackend, cfg: dict[str, Any], data: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    bus = Capture()
    service = LlmService(bus, backend, llm_cfg=cfg, logger=EventLogger("bench-llm", None, "ERROR"))
    feed(service, data["city"], case)
    job = service.next_job(0)
    t0 = time.perf_counter()
    outcome = service.process(job)
    latency = time.perf_counter() - t0
    proposals = bus.proposals()
    correct, safe = meets(proposals[-1] if proposals else None, case.get("expect", {}), data["city"])
    return {"case": case["id"], "outcome": outcome, "latency_s": latency, "correct": correct, "safe": safe,
            "injection": bool(case.get("injection")) or case["id"] == "B10"}


def bench_model(model: str, cfg: dict[str, Any], data: dict[str, Any], repeat: int) -> dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    if model != cfg["backend"]["model"]:
        cfg["backend"]["digest"] = ""                   # the pin belongs to the configured model only
    cfg["backend"]["model"] = model
    backend = OllamaBackend(cfg["backend"])
    ok, reason = backend.health()
    if not ok:
        return {"model": model, "error": reason}
    backend.warmup(timeout_s=300)                       # loading time is not answer time
    runs = [run_case(backend, cfg, data, case) for _ in range(repeat) for case in data["cases"]]
    lat = sorted(r["latency_s"] for r in runs)
    injection = [r for r in runs if r["injection"]]
    return {
        "model": model,
        "runs": len(runs),
        "median_s": statistics.median(lat),
        "p95_s": lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))],
        "max_s": lat[-1],
        "valid": sum(r["outcome"] == "proposal" for r in runs) / len(runs),
        "timeouts": sum(r["outcome"] == "timeout" for r in runs),
        "correct": sum(r["correct"] for r in runs) / len(runs),
        "per_case": {c["id"]: sum(r["correct"] for r in runs if r["case"] == c["id"]) / repeat for c in data["cases"]},
        "injection_runs": len(injection),
        "injection_safe": all(r["safe"] for r in injection) if injection else None,
    }


def table(results: list[dict[str, Any]], cases: list[str]) -> str:
    """One row per model; one column per case when there are few, else the list of misses."""
    if len(cases) > 10:
        return _wide_table(results)
    head = ("| Model | Runs | Median s | p95 s | Max s | Valid JSON | Timeouts | Correct | "
            + " | ".join(cases) + " | Injection safe |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['model']} | not run: {r['error']} |")
            continue
        per = " | ".join(f"{r['per_case'][c]:.0%}" for c in cases)
        lines.append(
            f"| {r['model']} | {r['runs']} | {r['median_s']:.2f} | {r['p95_s']:.2f} | {r['max_s']:.2f} | "
            f"{r['valid']:.0%} | {r['timeouts']} | {r['correct']:.0%} | {per} | "
            f"{'yes' if r['injection_safe'] else 'NO'} |")
    return "\n".join(lines)


def _wide_table(results: list[dict[str, Any]]) -> str:
    lines = ["| Model | Runs | Median s | p95 s | Max s | Valid JSON | Timeouts | Correct | Injection safe | Cases missed |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['model']} | not run: {r['error']} |")
            continue
        missed = ", ".join(f"{c} ({v:.0%})" for c, v in r["per_case"].items() if v < 1) or "none"
        lines.append(
            f"| {r['model']} | {r['runs']} | {r['median_s']:.2f} | {r['p95_s']:.2f} | {r['max_s']:.2f} | "
            f"{r['valid']:.0%} | {r['timeouts']} | {r['correct']:.0%} | "
            f"{'yes' if r['injection_safe'] else 'NO'} ({r['injection_runs']} runs) | {missed} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default=None, help="comma-separated Ollama model names (default: config)")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--json", type=Path, default=None, help="also write raw results here")
    args = ap.parse_args()
    cfg = config.load("llm")
    data = load_cases(args.cases)
    models = args.models.split(",") if args.models else [cfg["backend"]["model"]]
    results = [bench_model(m.strip(), cfg, data, args.repeat) for m in models]
    print(table(results, [c["id"] for c in data["cases"]]))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
