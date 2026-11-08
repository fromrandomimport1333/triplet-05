"""Case memory: "which past cases look like this moment?" (dataset studies in vector-storage/).

Independent of MQTT, like the other services: `handle(topic, payload, retained)` takes the internal
messages, `tick()` runs the lookups, and the service calls `publish`.

Per zone that has a sensor of every required type (config/memory.yaml): one value per second per
signal, kept for the baseline window. Every `every_s`, the last `window_s` seconds become the same
7 numbers as the NIST notebook (level above the sensor's own baseline, and trend, of smoke, CO, CO2
and ceiling temperature), encoded as signed logs over fixed scales. The `k` nearest library cases
(Euclidean) vote: quiet, nuisance (cooking) or fire; a verdict needs `min_share` of the votes,
otherwise the previous one stands (a ramp of smoke can look like either for a few seconds), and falling
smoke never starts a new verdict (the tail of an episode, e.g. once a fire is out). Safety first: a
fire verdict is never turned into "nuisance"; the zone must be quiet again before. The verdict goes to brain/internal/case
(detection reads it) and, when it changes, to the timeline.

The library is about 5,000 cases of 7 numbers: a plain search in memory takes a few milliseconds,
so no vector database runs on the edge device. Readings Guardian rejected are left out.
"""

from __future__ import annotations

import csv
import heapq
import json
import math
from collections import Counter, deque
from typing import Any, Callable

from ..common import config
from ..common.events import publish_event
from ..common.ids import IdGenerator
from ..common.log import EventLogger, utc_now_iso
from ..common.models import CaseMatch

Publisher = Callable[[str, str, int, bool], None]   # topic, payload, qos, retain
ORDER = {"quiet": 0, "nuisance": 1, "fire": 2}       # vote ties: the calmer verdict (as the notebook)


def encode(f: dict[str, float], features: list[str], scales: list[float]) -> list[float]:
    return [math.copysign(math.log1p(abs(f[k]) / s), f[k]) for k, s in zip(features, scales, strict=True)]


def load_library(path: str, features: list[str], scales: list[float],
                 include_holdout: bool = False) -> list[tuple[list[float], str, str]]:
    """(vector, state, source) per case of the CSV written by tools/build_cases.py."""
    out = []
    with open(config.resolve(path), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("holdout") == "1" and not include_holdout:
                continue
            out.append((encode({k: float(r[k]) for k in features}, features, scales), r["state"], r["source"]))
    if not out:
        raise ValueError(f"case library {path} is empty")
    return out


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(pos)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (pos - lo)


def window_features(win: dict[str, list[float]], base: dict[str, float]) -> dict[str, float]:
    """The notebook's 7 numbers from one value per second per signal (same as tools/build_cases.py)."""
    def lvl(c: str) -> float:
        v = win.get(c) or []
        return sum(v) / len(v) - base[c] if v and c in base else 0.0

    def slope(c: str) -> float:
        v = win.get(c) or []
        return (sum(v[-5:]) / 5 - sum(v[:5]) / 5) / max(len(v) - 5, 1) if len(v) > 10 else 0.0

    return {"obsc": max(lvl("obsc"), 0.0), "obsc_slope": slope("obsc"), "co": lvl("co"), "co_slope": slope("co"),
            "co2": lvl("co2"), "tc": lvl("tc"), "tc_slope": slope("tc")}


class MemoryService:
    def __init__(
        self,
        publish: Publisher,
        *,
        clock: Callable[[], float],
        logger: EventLogger | None = None,
        mem_cfg: dict[str, Any] | None = None,
        topics_cfg: dict[str, Any] | None = None,
        seal_keys: dict[str, bytes] | None = None,
        library: list[tuple[list[float], str, str]] | None = None,
    ):
        self.cfg = mem_cfg or config.load("memory")
        self.topics = topics_cfg or config.load("topics")
        self._publish = publish
        self.clock = clock
        log_cfg = self.cfg.get("logging", {})
        self.log = logger or EventLogger("brain-memory", log_cfg.get("file"), log_cfg.get("level", "INFO"))
        self.seal_keys = seal_keys
        self.ids = IdGenerator("mem")
        lib = self.cfg["library"]
        self.features = list(self.cfg["features"])
        self.scales = [float(x) for x in self.cfg["scales"]]
        self.library = library if library is not None else load_library(
            lib["file"], self.features, self.scales, bool(lib.get("include_holdout")))
        self.signal_of_type = {spec["type"]: (col, float(spec.get("factor", 1.0)))
                               for col, spec in self.cfg["signals"].items()}
        keep = float(self.cfg["baseline"]["window_s"])
        self._keep = max(keep, float(self.cfg["window_s"]))
        # city (from meta) and live state
        self.zone_names: dict[str, str] = {}
        self.sensor_zone: dict[str, tuple[str, str]] = {}            # sensor -> (zone, column)
        self.zones: dict[str, dict[str, list[str]]] = {}             # zone -> column -> sensors
        self.series: dict[str, deque[tuple[int, float]]] = {}        # sensor -> (second, value)
        self.last_corr: dict[str, str] = {}                          # zone -> newest reading's correlation id
        self.rejected: set[str] = set()
        self._last_eval: dict[str, float] = {}
        self._last: dict[str, tuple[str, float]] = {}                # zone -> (verdict, published at)

    # --- inputs ---------------------------------------------------------------------

    def subscriptions(self) -> list[tuple[str, int]]:
        t = self.topics["internal"]
        return [(t["meta"]["topic"], 1), (t["reading"]["topic"], 0),
                (t["trust"]["topic"].replace("{sensor_id}", "+"), 1)]

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        t = self.topics["internal"]
        try:
            if topic == t["reading"]["topic"]:
                self.on_reading(msg)
            elif topic == t["meta"]["topic"]:
                self.on_meta(msg)
            elif topic.startswith(t["trust"]["topic"].split("{")[0]):
                sid = msg["sensor_id"]
                if msg.get("verdict") == "rejected":
                    self.rejected.add(sid)
                    self.series.pop(sid, None)            # a rejected reading never shapes the baseline
                else:
                    self.rejected.discard(sid)
        except (KeyError, TypeError, ValueError) as e:
            self.log.warning("internal_message_refused", topic=topic, error=str(e)[:300])

    def on_meta(self, meta: dict[str, Any]) -> None:
        self.zone_names = {z["zone_id"]: z.get("name") or z["zone_id"] for z in meta["zones"]}
        self.sensor_zone, zones = {}, {}
        for s in meta["sensors"]:
            sig = self.signal_of_type.get(s["type"])
            if sig:
                self.sensor_zone[s["sensor_id"]] = (s["zone_id"], sig[0])
                zones.setdefault(s["zone_id"], {}).setdefault(sig[0], []).append(s["sensor_id"])
        required = set(self.cfg["required"])
        self.zones = {z: cols for z, cols in zones.items() if required <= set(cols)}
        self.log.info("meta_loaded", zones_assessed=sorted(self.zones), library_cases=len(self.library))

    def on_reading(self, msg: dict[str, Any]) -> None:
        sid = msg["sensor_id"]
        if sid not in self.sensor_zone or sid in self.rejected:
            return
        zone, col = self.sensor_zone[sid]
        factor = self.signal_of_type[msg.get("type")][1] if msg.get("type") in self.signal_of_type else 1.0
        now = self.clock()
        sec = int(now)
        d = self.series.setdefault(sid, deque())
        value = float(msg["value"]) * factor
        if d and d[-1][0] == sec:
            d[-1] = (sec, value)                          # one value per second: the newest
        else:
            d.append((sec, value))
        while d and d[0][0] <= sec - self._keep:
            d.popleft()
        if msg.get("correlation_id"):
            self.last_corr[zone] = msg["correlation_id"]

    # --- lookups --------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        for zone in sorted(self.zones):
            if now - self._last_eval.get(zone, float("-inf")) < float(self.cfg["every_s"]):
                continue
            self._last_eval[zone] = now
            self.assess(zone, now)

    def window(self, zone: str, now: float) -> tuple[dict[str, list[float]], dict[str, float], int, list[str]]:
        """(values per column in the window, baseline per column, seconds with data, sensors read)."""
        cfg_b = self.cfg["baseline"]
        start = int(now) - int(self.cfg["window_s"])
        win: dict[str, list[float]] = {}
        base: dict[str, float] = {}
        seconds: set[int] = set()
        used: list[str] = []
        for col, sensors in self.zones[zone].items():
            for sid in sensors:                               # first live sensor of the type
                d = self.series.get(sid)
                if not d:
                    continue
                vals = [(s, v) for s, v in d if s > start]
                if not vals:
                    continue
                hist = [v for s, v in d if s > int(now) - int(cfg_b["window_s"])]
                if len(hist) >= int(cfg_b["min_samples"]):
                    base[col] = _quantile(hist, float(cfg_b["quantile"]))
                win[col] = [v for _, v in vals]
                seconds.update(s for s, _ in vals)
                used.append(sid)
                break
        return win, base, len(seconds), used

    def nearest(self, vec: list[float]) -> list[tuple[str, str]]:
        k = int(self.cfg["k"])
        best = heapq.nsmallest(k, self.library,
                               key=lambda e: sum((a - b) * (a - b) for a, b in zip(vec, e[0], strict=True)))
        return [(state, source) for _, state, source in best]

    def assess(self, zone: str, now: float) -> CaseMatch | None:
        win, base, n, used = self.window(zone, now)
        required = set(self.cfg["required"])
        if n < int(self.cfg["min_samples"]) or not required <= set(win) or not required <= set(base):
            return None
        f = window_features(win, base)
        q = self.cfg["quiet"]
        k = int(self.cfg["k"])
        if f["obsc"] < float(q["obsc"]) and f["co"] < float(q["co"]) and f["tc"] < float(q["tc"]):
            verdict, votes, sources = "quiet", {"quiet": k}, {}
        else:
            nn = self.nearest(encode(f, self.features, self.scales))
            votes = dict(Counter(s for s, _ in nn))
            verdict = max(votes, key=lambda v: (votes[v], -ORDER[v]))
            sources = dict(Counter(src for s, src in nn if s == verdict).most_common())
        share = votes[verdict] / k
        if verdict != "quiet" and share < float(self.cfg.get("min_share", 0)):
            self.log.info("case_uncertain", zone_id=zone, votes=votes)   # no clear majority: keep the last verdict
            return None
        msg = CaseMatch(id=self.ids.next(), timestamp=utc_now_iso(), correlation_id=self.last_corr.get(zone),
                        zone_id=zone, incident_type=self.cfg["library"]["incident_type"], verdict=verdict,
                        share=round(share, 3), k=k, votes=votes, sources=sources,
                        features={c: round(v, 4) for c, v in f.items()}, sensors=used,
                        library=self.cfg["library"]["name"], summary=self.summary(zone, verdict, votes, k, sources, f))
        prev = self._last.get(zone)
        falling = f["obsc_slope"] < -float(self.cfg.get("falling_slope", 0.005))
        if verdict != "quiet" and (prev is None or prev[0] == "quiet") and falling:
            return None                                   # falling smoke ends an episode, it never starts one
        if verdict == "nuisance" and prev is not None and prev[0] == "fire":
            self.log.info("case_fire_kept", zone_id=zone, votes=votes)
            return None                                   # never talks a fire down: quiet first, then a new episode
        changed = prev is None or prev[0] != verdict
        due = verdict != "quiet" and (changed or now - prev[1] >= float(self.cfg["repeat_s"]))
        if changed and (verdict != "quiet" or prev is not None):
            self.log.info("case_verdict", zone_id=zone, verdict=verdict, share=msg.share, votes=votes,
                          sources=sources, features=msg.features)
        if due or (changed and prev is not None):
            spec = self.topics["internal"]["case"]
            self._publish(spec["topic"], msg.to_json(), int(spec["qos"]), bool(spec["retain"]))
            self._last[zone] = (verdict, now)
            if changed and verdict != "quiet":
                publish_event(self._publish, self.topics, self.ids, self.seal_keys, f"case_{verdict}",
                              msg.summary, correlation_id=msg.correlation_id)
        return msg

    def summary(self, zone: str, verdict: str, votes: dict[str, int], k: int, sources: dict[str, int],
                f: dict[str, float]) -> str:
        where = f"{zone} {self.zone_names[zone]}" if self.zone_names.get(zone, zone) != zone else zone
        like = ", ".join(f"{s} {n}" for s, n in list(sources.items())[:3])
        reading = (f"smoke {f['obsc']:.2f} %/ft, CO {f['co']:+.1f} ppm, CO2 {f['co2']:+.3f} %, "
                   f"ceiling {f['tc']:+.1f} C")
        n = votes.get(verdict, 0)
        if verdict == "fire":
            return f"{where}: looks like a REAL FIRE, {n} of {k} most similar lab cases are fires ({like}); {reading}"
        if verdict == "nuisance":
            return (f"{where}: smoke looks like COOKING, not a fire: {n} of {k} most similar lab cases are cooking "
                    f"({like}); {reading}")
        return f"{where}: back to quiet; {reading}"
