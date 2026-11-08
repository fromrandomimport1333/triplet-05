"""Scenario files: load, validate, plan and evaluate. Pure functions, no broker.

A scenario file `scenarios/<ID>.yaml` holds the official Test Book fields (id, name, layers,
description, trigger, expected {twin, guardian, brain}, how_to_verify) and the machine fields:

    duration_s: 60                          # <= 90
    observe: {readings: [water-01]}         # readings to record (verdicts, commands, events always are)
    twin:
      reset: {seed: 7, scenario: flood, scenario_zone: zone-01, scenario_after_ticks: 5,
              rush_hour: null, calls: []}   # one `reset` control message; Twin ticks count from it
      faults: [{mode: falsify, tick: 10, end_tick: 80, device_id: water-01, value: 1.8}]
      actuator_modes: {valve-01: stuck}
      actions:                              # control messages at time offsets (seconds after start)
        - {at_s: 8, op: trigger, scenario: accident, zone: zone-03}
        - {at_s: 5, op: call, zone_id: zone-03, category: fire, text: "...", repeat: {count: 10, every_s: 6}}
    guardian: {modes: [auto], actions: [{at_s: 3, mode: offline}]}
    llm:      {mode: scripted, actions: [{at_s: 0, mode: offline}]}
    expect:                                 # checks over the messages observed during the run
      - {event: incident_opened, match: {zone_id: zone-01, type: flood}, within_s: 20}
      - {no_command: {target_type: valve}}

Check entry: one kind key (see KINDS, `no_<kind>`, `max_commands`, `any_of`) whose value is a
name (or a list of names) or a dict of filters, plus optional `match`, `text`, `after_s`,
`within_s`, `count` and `optional` (reported, never fails the run). Filters compare body fields
(`zone` = `zone_id`); a list means "one of"; a dict {lt, le, gt, ge} compares numbers;
`target_type` / `target_id` / `action` / `target_zone` look at the command, at each action of a
decision or proposal, or at the target of an outcome, alert or acknowledgement; `text` is a
case-insensitive substring of the whole body.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCENARIO_DIR.parent
BRAIN_DIR = REPO_ROOT / "brain"
if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

from mocks.guardian.core import parse_mode  # noqa: E402
from mocks.mock_llm.backend import MODES as LLM_MODES  # noqa: E402
from mocks.twin.city import load_city  # noqa: E402
from mocks.twin.core import ACTUATOR_MODES, CALL_CATEGORIES, FAULT_MODES, SCENARIOS  # noqa: E402

TESTBOOK_FIELDS = ("id", "name", "layers", "description", "trigger", "expected", "how_to_verify")
LAYERS = ("twin", "guardian", "brain")
MAX_DURATION_S = 90
TWIN_OPS = ("trigger", "call", "contain", "faults", "offline", "online")
RESET_KEYS = ("seed", "scenario", "scenario_zone", "scenario_after_ticks", "rush_hour", "calls")
RESET_DEFAULTS = {"seed": 7, "scenario": "calm", "scenario_zone": None, "scenario_after_ticks": 5,
                  "rush_hour": None, "calls": []}
ENTRY_KEYS = ("match", "text", "after_s", "within_s", "count", "optional", "note")
TARGET_FILTERS = ("target_type", "target_id", "action", "target_zone")
ALIASES = {"zone": "zone_id"}

# kind -> topic prefixes it is read from, and the body field its short name refers to
KINDS: dict[str, dict[str, Any]] = {
    "event":      {"topics": ("brain/event",), "name": "event"},
    "error_code": {"topics": ("brain/error",), "name": "code"},
    "alert":      {"topics": ("brain/alert",), "name": "alert"},
    "command":    {"topics": ("brain/cmd/",), "name": "action"},
    "incident":   {"topics": ("brain/internal/incident",), "name": "type"},
    "decision":   {"topics": ("brain/internal/decision",), "name": "origin"},
    "outcome":    {"topics": ("brain/internal/outcome",), "name": "status"},
    "proposal":   {"topics": ("brain/internal/proposal",), "name": "origin"},
    "layer":      {"topics": ("brain/internal/layer-status/",), "name": "layer"},
    "verdict":    {"topics": ("guardian/verdict/",), "name": "verdict"},
    "quarantine": {"topics": ("guardian/quarantine/",), "name": "state"},
    "ack":        {"topics": ("twin/actuator/", "twin/unit/"), "name": "result"},
    "reading":    {"topics": ("city/",), "name": "sensor_id"},
}
CHECK_KINDS = (*KINDS, *(f"no_{k}" for k in KINDS), "max_commands", "any_of")

# Topics the runner subscribes to (readings only for the sensors a scenario asks for)
OBSERVED_TOPICS = (
    "brain/event", "brain/error", "brain/alert", "brain/cmd/+", "brain/status",
    "brain/internal/incident", "brain/internal/decision", "brain/internal/outcome",
    "brain/internal/proposal", "brain/internal/layer-status/+", "brain/internal/llm-status",
    "guardian/verdict/+", "guardian/quarantine/+", "guardian/status",
    "twin/actuator/+", "twin/unit/+", "twin/status", "city/+/call",
)


class ScenarioError(ValueError):
    pass


# --- city -----------------------------------------------------------------------------


@dataclass
class CityIndex:
    zones: set[str]
    sensors: dict[str, dict[str, Any]]
    targets: dict[str, dict[str, Any]]          # actuator or unit id -> {type, zone_id}

    @property
    def target_types(self) -> set[str]:
        return {t["type"] for t in self.targets.values()}

    @property
    def devices(self) -> set[str]:
        return set(self.sensors) | set(self.targets)


def city_index(path: str | Path | None = None) -> CityIndex:
    """The mock city (brain/mocks/city.yaml), or a city of the same format."""
    city = load_city(path)
    targets = {a: {"type": s["type"], "zone_id": s["zone_id"]} for a, s in city.actuators.items()}
    targets.update({u: {"type": s["type"], "zone_id": s["home_zone"]} for u, s in city.units.items()})
    return CityIndex(set(city.zone_ids), dict(city.sensors), targets)


def city_from_meta(meta: dict[str, Any]) -> CityIndex:
    """Same index from brain/internal/meta (retained), when the real Twin's city is in use."""
    targets = {a["actuator_id"]: {"type": a["type"], "zone_id": a["zone_id"]} for a in meta.get("actuators", [])}
    targets.update({u["unit_id"]: {"type": u["type"], "zone_id": u.get("home_zone")} for u in meta.get("units", [])})
    return CityIndex({z["zone_id"] for z in meta.get("zones", [])},
                     {s["sensor_id"]: s for s in meta.get("sensors", [])}, targets)


# --- loading --------------------------------------------------------------------------


def scenario_path(scenario_id: str) -> Path:
    return SCENARIO_DIR / f"{scenario_id}.yaml"


def list_scenarios() -> list[str]:
    """Scenario ids, B1..B13 in numeric order, then the others."""
    def key(i: str) -> tuple:
        digits = "".join(c for c in i if c.isdigit())
        return (i.rstrip("0123456789"), int(digits) if digits else 0, i)
    return sorted((p.stem for p in SCENARIO_DIR.glob("*.yaml")), key=key)


def load_scenario(ref: str | Path) -> dict[str, Any]:
    """By id (B3) or path. `alias_of: B3` reuses another scenario and overrides its Test Book fields."""
    path = Path(ref) if str(ref).endswith((".yaml", ".yml")) else scenario_path(str(ref))
    if not path.is_file():
        raise ScenarioError(f"no scenario file {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path.name}: not a mapping")
    if "alias_of" in raw:
        base = load_scenario(raw["alias_of"])
        if "alias_of" in base:
            raise ScenarioError(f"{path.name}: alias of an alias")
        raw = {**base, **raw}
    return raw


# --- validation -----------------------------------------------------------------------


def validate(sc: dict[str, Any], city: CityIndex) -> list[str]:
    """Problems of one scenario (empty list = valid)."""
    p: list[str] = []
    sid = sc.get("id", "?")
    for f in TESTBOOK_FIELDS:
        if f not in sc or sc[f] in (None, "", [], {}):
            p.append(f"missing Test Book field {f!r}")
    if not isinstance(sc.get("layers"), list) or any(x not in LAYERS for x in sc.get("layers") or []):
        p.append(f"layers must be a list of {LAYERS}")
    exp = sc.get("expected")
    if not isinstance(exp, dict) or any(not exp.get(layer) for layer in LAYERS):
        p.append("expected needs twin, guardian and brain")
    if sc.get("trigger") != f"python scenarios/run.py {sid}":
        p.append(f"trigger must be 'python scenarios/run.py {sid}'")
    dur = sc.get("duration_s")
    if not isinstance(dur, (int, float)) or not 0 < dur <= MAX_DURATION_S:
        p.append(f"duration_s must be in (0, {MAX_DURATION_S}]")
        dur = MAX_DURATION_S
    p += _validate_twin(sc.get("twin") or {}, city, dur)
    p += _validate_guardian(sc.get("guardian") or {}, city, dur)
    p += _validate_llm(sc.get("llm") or {}, dur)
    for r in (sc.get("observe") or {}).get("readings") or []:
        if r not in city.sensors:
            p.append(f"observe: unknown sensor {r!r}")
    expect = sc.get("expect")
    if not isinstance(expect, list) or not expect:
        p.append("expect must be a non-empty list")
    else:
        for i, entry in enumerate(expect):
            p += [f"expect[{i}]: {e}" for e in _validate_entry(entry, city, dur)]
    return [f"{sid}: {x}" for x in p]


def _at(action: dict[str, Any], dur: float) -> list[str]:
    at = action.get("at_s")
    if not isinstance(at, (int, float)) or not 0 <= at <= dur:
        return [f"at_s must be within the duration, got {at!r}"]
    rep = action.get("repeat")
    if rep is not None:
        if not isinstance(rep, dict) or not isinstance(rep.get("count"), int) or rep["count"] < 1 \
                or not isinstance(rep.get("every_s"), (int, float)) or rep["every_s"] <= 0:
            return ["repeat needs count >= 1 and every_s > 0"]
        if at + rep["every_s"] * (rep["count"] - 1) > dur:
            return ["repeat goes past the duration"]
    return []


def _validate_twin(tw: dict[str, Any], city: CityIndex, dur: float) -> list[str]:
    p = []
    reset = tw.get("reset") or {}
    for k in reset:
        if k not in RESET_KEYS:
            p.append(f"twin.reset: unknown key {k!r}")
    if reset.get("scenario", "calm") not in SCENARIOS:
        p.append(f"twin.reset: unknown scenario {reset.get('scenario')!r}")
    if reset.get("scenario_zone") is not None and reset["scenario_zone"] not in city.zones:
        p.append(f"twin.reset: unknown zone {reset['scenario_zone']!r}")
    for c in reset.get("calls") or []:
        if c.get("zone_id") not in city.zones or c.get("category") not in CALL_CATEGORIES:
            p.append(f"twin.reset.calls: bad zone or category in {c!r}")
    for f in tw.get("faults") or []:
        if f.get("mode") not in FAULT_MODES:
            p.append(f"twin.faults: unknown mode {f.get('mode')!r}")
        if f.get("device_id") is not None and f["device_id"] not in city.devices:
            p.append(f"twin.faults: unknown device {f['device_id']!r}")
        if not isinstance(f.get("tick"), int):
            p.append(f"twin.faults: tick must be an integer in {f!r}")
    for dev, mode in (tw.get("actuator_modes") or {}).items():
        if dev not in city.targets or mode not in ACTUATOR_MODES:
            p.append(f"twin.actuator_modes: bad {dev}: {mode}")
    for a in tw.get("actions") or []:
        p += [f"twin.actions: {e}" for e in _at(a, dur)]
        op = a.get("op")
        if op not in TWIN_OPS:
            p.append(f"twin.actions: unknown op {op!r}")
        elif op == "trigger" and (a.get("scenario") not in SCENARIOS or a.get("zone", "zone-01") not in city.zones):
            p.append(f"twin.actions: bad trigger {a!r}")
        elif op == "call" and (a.get("zone_id") not in city.zones or a.get("category") not in CALL_CATEGORIES
                               or not isinstance(a.get("text"), str)):
            p.append(f"twin.actions: bad call {a!r}")
        elif op == "contain" and (a.get("device_id") not in city.devices
                                  or a.get("action") not in ("isolate", "quarantine", "release")):
            p.append(f"twin.actions: bad contain {a!r}")
    return p


def _modes(a: dict[str, Any]) -> list[str]:
    return list(a["modes"]) if "modes" in a else [a["mode"]] if "mode" in a else []


def _validate_guardian(gd: dict[str, Any], city: CityIndex, dur: float) -> list[str]:
    p = []
    for spec in [*(gd.get("modes") or []), *(m for a in gd.get("actions") or [] for m in _modes(a))]:
        try:
            name, target = parse_mode(spec)
        except ValueError as e:
            p.append(f"guardian: {e}")
            continue
        if target is not None and target not in (city.devices if name in ("quarantine", "release") else city.sensors):
            p.append(f"guardian: unknown device in {spec!r}")
    for a in gd.get("actions") or []:
        p += [f"guardian.actions: {e}" for e in _at(a, dur)]
        if not _modes(a) and a.get("op") != "reset":
            p.append(f"guardian.actions: needs mode, modes or op: reset in {a!r}")
    return p


def _validate_llm(llm: dict[str, Any], dur: float) -> list[str]:
    p = []
    for m in [llm.get("mode", "scripted"), *(a.get("mode") for a in llm.get("actions") or [])]:
        if m not in LLM_MODES:
            p.append(f"llm: unknown mode {m!r}")
    for a in llm.get("actions") or []:
        p += [f"llm.actions: {e}" for e in _at(a, dur)]
    return p


def _validate_entry(entry: Any, city: CityIndex, dur: float) -> list[str]:
    if not isinstance(entry, dict):
        return ["not a mapping"]
    kinds = [k for k in entry if k in CHECK_KINDS]
    others = [k for k in entry if k not in CHECK_KINDS and k not in ENTRY_KEYS]
    if len(kinds) != 1 or others:
        return [f"needs exactly one check kind of {CHECK_KINDS} and only {ENTRY_KEYS}, got {list(entry)}"]
    kind = kinds[0]
    p = []
    for k in ("after_s", "within_s"):
        if k in entry and not (isinstance(entry[k], (int, float)) and 0 <= entry[k] <= dur):
            p.append(f"{k} must be within the duration")
    if kind == "any_of":
        subs = entry["any_of"]
        if not isinstance(subs, list) or len(subs) < 2:
            return ["any_of needs a list of at least two checks"]
        for s in subs:
            p += _validate_entry(s, city, dur)
        return p
    spec = _spec(entry, kind)
    if kind == "max_commands" and not isinstance(spec.get("count"), int):
        p.append("max_commands needs an integer count")
    for k, v in spec.items():
        values = v if isinstance(v, list) else [v]
        if k == "target_type" and any(x not in city.target_types for x in values):
            p.append(f"unknown target_type {v!r}")
        elif k == "target_id" and any(x not in city.targets for x in values):
            p.append(f"unknown target_id {v!r}")
        elif k in ("zone_id", "zone", "target_zone") and any(x not in city.zones for x in values):
            p.append(f"unknown zone {v!r}")
        elif k in ("sensor_id",) and any(x not in city.sensors for x in values):
            p.append(f"unknown sensor {v!r}")
        elif k == "device_id" and any(x not in city.devices for x in values):
            p.append(f"unknown device {v!r}")
    base = kind.removeprefix("no_")
    if base == "reading" and "sensor_id" not in spec:
        p.append("a reading check needs a sensor")
    return p


# --- plan -----------------------------------------------------------------------------


@dataclass(order=True)
class Step:
    at_s: float
    order: int
    channel: str = field(compare=False)      # twin | guardian | llm
    body: dict[str, Any] = field(compare=False)


def _expand(actions: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    out = []
    for a in actions or []:
        body = {k: v for k, v in a.items() if k not in ("at_s", "repeat")}
        rep = a.get("repeat") or {"count": 1, "every_s": 1}
        for i in range(rep["count"]):
            out.append((float(a["at_s"]) + i * float(rep["every_s"]), dict(body)))
    return out


def plan(sc: dict[str, Any]) -> list[Step]:
    """Control messages in sending order. Start: LLM mode, Guardian reset, Twin reset (+ faults)."""
    steps: list[Step] = []

    def add(at: float, channel: str, body: dict[str, Any]) -> None:
        steps.append(Step(at, len(steps), channel, body))

    llm, gd, tw = sc.get("llm") or {}, sc.get("guardian") or {}, sc.get("twin") or {}
    add(0, "llm", {"mode": llm.get("mode", "scripted")})
    add(0, "guardian", {"op": "reset", "modes": list(gd.get("modes") or ["auto"])})
    add(0, "twin", {"op": "reset", **RESET_DEFAULTS, **(tw.get("reset") or {})})
    if tw.get("faults") or tw.get("actuator_modes"):
        add(0, "twin", {"op": "faults", "faults": list(tw.get("faults") or []),
                        "actuator_modes": dict(tw.get("actuator_modes") or {})})
    for at, body in _expand(tw.get("actions")):
        add(at, "twin", body)
    for at, body in _expand(gd.get("actions")):
        add(at, "guardian", {"op": "reset"} if body.get("op") == "reset" else {"op": "mode", "modes": _modes(body)})
    for at, body in _expand(llm.get("actions")):
        add(at, "llm", {"mode": body["mode"]})
    return sorted(steps)


def cleanup_steps() -> list[Step]:
    """Sent after the run: Guardian back to its config, mock LLM back to scripted."""
    return [Step(0, 0, "guardian", {"op": "reset"}), Step(0, 1, "llm", {"mode": "scripted"})]


def reading_subscriptions(sc: dict[str, Any], city: CityIndex) -> list[str]:
    sensors = set((sc.get("observe") or {}).get("readings") or [])
    for entry in sc.get("expect") or []:
        for e in entry.get("any_of", [entry]):
            if any(k.removeprefix("no_") == "reading" for k in e):
                sensors.add(_spec(e, next(k for k in e if k in CHECK_KINDS))["sensor_id"])
    return sorted(f"city/{city.sensors[s]['zone_id']}/sensor/{city.sensors[s]['type']}/{s}"
                  for s in sensors if s in city.sensors)


# --- evaluation -----------------------------------------------------------------------


@dataclass
class Result:
    check: str
    passed: bool
    detail: str
    optional: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail, "optional": self.optional}


def decode(payload: bytes | str) -> dict[str, Any] | None:
    """Body of an MQTT payload: the inner body of an envelope, or the JSON object itself."""
    try:
        msg = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(msg, dict) and msg.get("v") == 1 and isinstance(msg.get("body"), str):
        try:
            msg = json.loads(msg["body"])
        except ValueError:
            return None
    return msg if isinstance(msg, dict) else None


def _spec(entry: dict[str, Any], kind: str) -> dict[str, Any]:
    v = entry[kind]
    base = kind.removeprefix("no_")
    name_field = KINDS[base]["name"] if base in KINDS else None
    if v is None or v == "any":
        spec: dict[str, Any] = {}
    elif isinstance(v, dict):
        spec = dict(v)
    else:
        spec = {name_field: v} if name_field else {}
    spec.update(entry.get("match") or {})
    return {ALIASES.get(k, k): val for k, val in spec.items()}


def _records(kind: str, observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live (not retained) messages of a kind, with `t` and `topic`. Incidents also count as
    events incident_<status>, so `event: incident_opened` works whoever publishes it."""
    prefixes = KINDS[kind]["topics"]
    out = []
    for m in observed:
        if m.get("retained") or not isinstance(m.get("body"), dict):
            continue
        topic = m["topic"]
        if any(topic == p or (p.endswith("/") and topic.startswith(p)) for p in prefixes):
            if kind == "reading" and "/sensor/" not in topic:
                continue
            out.append(m)
        elif kind == "event" and topic == "brain/internal/incident" and "status" in m["body"]:
            out.append({**m, "body": {**m["body"], "event": f"incident_{m['body']['status']}"}})
    return out


def _value_ok(actual: Any, wanted: Any) -> bool:
    if isinstance(wanted, list):
        return any(_value_ok(actual, w) for w in wanted)
    if isinstance(wanted, dict):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        ops = {"lt": actual.__lt__, "le": actual.__le__, "gt": actual.__gt__, "ge": actual.__ge__}
        return all(k in ops and ops[k](v) for k, v in wanted.items())
    return actual == wanted


def _targets(kind: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """(target_id, action, zone) seen in a body, for target filters."""
    if kind in ("decision", "proposal"):
        return [{"target_id": a.get("target_id"), "action": a.get("action"),
                 "zone": (a.get("params") or {}).get("zone_id")} for a in body.get("actions") or []]
    tid = body.get("target_id") or body.get("actuator_id") or body.get("unit_id") or body.get("device_id")
    zone = (body.get("parameters") or {}).get("zone_id") if kind == "command" else None
    return [{"target_id": tid, "action": body.get("action"), "zone": zone}]


def matches(kind: str, body: dict[str, Any], spec: dict[str, Any], city: CityIndex, text: str | None = None) -> bool:
    if text and text.lower() not in json.dumps(body, ensure_ascii=False).lower():
        return False
    filters = {k: v for k, v in spec.items() if k in TARGET_FILTERS}
    for k, v in spec.items():
        if k in TARGET_FILTERS:
            continue
        if not _value_ok(body.get(k), v):
            return False
    if not filters:
        return True
    for t in _targets(kind, body):
        info = city.targets.get(t["target_id"] or "", {})
        seen = {"target_id": t["target_id"], "action": t["action"], "target_type": info.get("type"),
                "target_zone": t["zone"] or info.get("zone_id")}
        if all(_value_ok(seen[k], v) for k, v in filters.items()):
            return True
    return False


def _describe(entry: dict[str, Any]) -> str:
    parts = []
    for k, v in entry.items():
        if k in ("optional", "note"):
            continue
        parts.append(f"{k}={json.dumps(v, separators=(',', ':'))}" if not isinstance(v, str) else f"{k}={v}")
    return " ".join(parts)


def evaluate_entry(entry: dict[str, Any], observed: list[dict[str, Any]], city: CityIndex,
                   duration_s: float) -> Result:
    kind = next(k for k in entry if k in CHECK_KINDS)
    desc = _describe(entry)
    optional = bool(entry.get("optional"))
    if kind == "any_of":
        window = {k: entry[k] for k in ("after_s", "within_s", "text") if k in entry}
        subs = [evaluate_entry({**window, **e}, observed, city, duration_s) for e in entry["any_of"]]
        ok = [s for s in subs if s.passed]
        detail = f"passed: {ok[0].check} ({ok[0].detail})" if ok else "none passed: " + "; ".join(s.detail for s in subs)
        return Result(desc, bool(ok), detail, optional)
    after = float(entry.get("after_s", 0))
    within = float(entry.get("within_s", duration_s + 5))
    spec = _spec(entry, kind)
    base = "command" if kind == "max_commands" else kind.removeprefix("no_")
    limit = int(spec.pop("count", 0)) if kind == "max_commands" else 0
    candidates = [m for m in _records(base, observed) if after <= m["t"] <= within]
    found = [m for m in candidates if matches(base, m["body"], spec, city, entry.get("text"))]
    window = f"in [{after:g}, {within:g}] s"
    if kind == "max_commands":
        ids = {m["body"].get("id") or f"{m['topic']}@{m['t']}" for m in found}   # a retry keeps its id
        n = len(ids)
        return Result(desc, n <= limit, f"{n} command(s) {window}, at most {limit}", optional)
    if kind.startswith("no_"):
        if found:
            m = found[0]
            return Result(desc, False, f"seen at {m['t']:.1f} s on {m['topic']}: {_short(m['body'])}", optional)
        return Result(desc, True, f"none {window} ({len(candidates)} {base} message(s) seen)", optional)
    need = int(entry.get("count", 1))
    if len(found) >= need:
        m = found[need - 1]
        return Result(desc, True, f"seen at {m['t']:.1f} s on {m['topic']}" + (f" ({len(found)} total)" if need > 1 else ""), optional)
    return Result(desc, False, f"{len(found)} of {need} {window} ({len(candidates)} {base} message(s) seen)", optional)


def evaluate(sc: dict[str, Any], observed: list[dict[str, Any]], city: CityIndex) -> list[Result]:
    dur = float(sc.get("duration_s", MAX_DURATION_S))
    return [evaluate_entry(e, observed, city, dur) for e in sc["expect"]]


def overall(results: list[Result]) -> bool:
    return all(r.passed or r.optional for r in results)


def _short(body: dict[str, Any], limit: int = 160) -> str:
    keep = {k: v for k, v in body.items() if k not in ("id", "timestamp", "source")}
    text = json.dumps(keep, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def report_lines(sc: dict[str, Any], results: list[Result]) -> list[str]:
    lines = [f"Scenario {sc['id']}: {sc['name']}"]
    for r in results:
        mark = "PASS" if r.passed else ("WARN" if r.optional else "FAIL")
        lines.append(f"  [{mark}] {r.check}\n         {r.detail}")
    lines.append(f"Result {sc['id']}: {'PASS' if overall(results) else 'FAIL'}")
    return lines
