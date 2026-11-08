"""Build the graph's incident history from a real storm: NYC 311 calls, Hurricane Ida (2021).

    python tools/build_history.py --csv path/to/311_ida.csv          # from brain/
    python tools/build_history.py --csv ... --cypher                 # also write the Neo4j load script

Port of the two notebooks in brain/notebooks/ (preprocessing for Qdrant, then the Neo4j graph),
in plain Python so it runs with the Brain's own dependencies and without any cloud service:

1. clean the 311 extract (timestamps, call types, boroughs, coordinates, duplicates);
2. per call, the city-wide number of similar calls in the previous 60 min -> surge level
   (isolated report / small cluster / busy period / heavy surge / extreme surge);
3. hourly windows, then surge waves (Episode): hours with >= 50 flood calls, merged when at
   most 2 h apart;
4. co-surge: how much more often traffic signals fail per hour inside a flood wave;
5. per incident type and surge level: how many calls, what share came inside a wave.

Writes data/history/nyc311-ida.json (small, read by the graph module at start, see
src/graph/history.py) and, with --cypher, data/history/nyc311-ida.cypher: the notebook's graph
(Call, CallType, IncidentType, Window, Episode, Zone, Borough) as one script for Neo4j
(`cypher-shell -f nyc311-ida.cypher`, or paste it in Neo4j Browser).

The Qdrant step of the notebook embeds a templated sentence ("<call type> reported. Situation:
<surge level>, <n> similar calls ..."): its nearest neighbours are the calls of the same type
and a close count. The graph module matches on exactly that (type + surge level) instead of
embeddings, so the Brain needs no vector database, model download or network at the demo.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import statistics
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

BRAIN = Path(__file__).resolve().parents[1]
OUT_DIR = BRAIN / "data" / "history"

TZ = ZoneInfo("America/New_York")                      # the CSV is in NYC local time
STORM_START = dt.datetime(2021, 9, 1, 18, tzinfo=TZ)     # Ida night (same window as the notebooks)
STORM_END = dt.datetime(2021, 9, 2, 6, tzinfo=TZ)
NYC_LAT, NYC_LON = (40.40, 41.00), (-74.35, -73.60)      # sanity box for coordinates
GRID_DEG = 0.01                                          # ~1.1 km cells -> zone id
EPISODE_MIN = 50                                         # >= this many flood calls in an hour = busy hour
MERGE_GAP_H = 2                                          # busy hours at most this far apart = same wave
DS = "nyc311-ida"

# descriptor -> (call_type, incident_type, call_group, label)   (notebook 1, section 3)
CALL_TYPES = {
    "Sewer Backup (Use Comments) (SA)": ("sewer_backup", "flood", "Sewer backup", "Sewer backup"),
    "Street Flooding (SJ)": ("street_flooding", "flood", "Other flooding", "Street flooding"),
    "Catch Basin Clogged/Flooding (Use Comments) (SC)": ("catch_basin", "flood", "Other flooding", "Clogged catch basin"),
    "Excessive Water In Basement (WEFB)": ("basement_water", "flood", "Other flooding", "Water in basement"),
    "Controller": ("signal_controller", "traffic_signal_failure", "Traffic signal failure", "Traffic signal failure"),
}
# surge level from the city-wide count of similar calls in the previous hour (upper bound inclusive)
SURGE_LEVELS = [(4, "isolated report"), (29, "small cluster"), (99, "busy period"), (299, "heavy surge"),
                (None, "extreme surge")]


def surge_level(n: int) -> str:
    for top, label in SURGE_LEVELS:
        if top is None or n <= top:
            return label
    raise AssertionError


def num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def pandas_float(x: float | None) -> str:
    """The float text pandas' astype('string') gives (notebook point ids are built from it)."""
    return "na" if x is None else repr(x)


# --- 1. load and clean ------------------------------------------------------------------------


def load(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    unmapped = {r["descriptor"] for r in raw} - set(CALL_TYPES)
    if unmapped:
        raise SystemExit(f"unmapped descriptors: {sorted(unmapped)}")
    calls = []
    for r in raw:
        call_type, incident_type, group, label = CALL_TYPES[r["descriptor"]]
        lat, lon = num(r["latitude"]), num(r["longitude"])
        if not (lat is not None and lon is not None and NYC_LAT[0] <= lat <= NYC_LAT[1] and NYC_LON[0] <= lon <= NYC_LON[1]):
            lat = lon = None
        borough = (r["borough"] or "").strip().title() or None
        zip_ = num(r["incident_zip"])
        calls.append({
            "created_at": dt.datetime.fromisoformat(r["created_date"]).replace(tzinfo=TZ),
            "descriptor": r["descriptor"], "call_type": call_type, "incident_type": incident_type,
            "call_group": group, "call_label": label,
            "borough": None if borough == "Unspecified" else borough,
            "zip_code": f"{int(zip_):05d}" if zip_ is not None else None,
            "lat": lat, "lon": lon, "has_geo": lat is not None,
        })
    # borough inferred from the nearest call that has one (only for calls with coordinates)
    known = [c for c in calls if c["borough"] and c["has_geo"]]
    for c in calls:
        if c["borough"] is None and c["has_geo"]:
            near = min(known, key=lambda k: (k["lat"] - c["lat"]) ** 2 + (k["lon"] - c["lon"]) ** 2)
            c["borough"] = near["borough"]
    # exact duplicates: dropped only when they have coordinates (same minute + same place = same report)
    seen: set[tuple] = set()
    out = []
    for c in calls:
        k = (c["created_at"], c["descriptor"], c["borough"], c["zip_code"], c["lat"], c["lon"])
        if k in seen and c["has_geo"]:
            continue
        seen.add(k)
        out.append(c)
    out.sort(key=lambda c: c["created_at"])              # stable, like the notebook
    return out


# --- 2. features ----------------------------------------------------------------------------


def features(calls: list[dict]) -> None:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    for c in calls:
        t = c["created_at"]
        c["ts"] = int((t - epoch).total_seconds())
        c["phase"] = "pre_storm" if t < STORM_START else "storm_night" if t < STORM_END else "aftermath"
        c["window"] = t.strftime("%Y-%m-%dT%H")
        c["zone"] = (f"z{int(c['lat'] // GRID_DEG)}_{int(c['lon'] // GRID_DEG)}" if c["has_geo"] else None)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        by_type[c["incident_type"]].append(c)
    for group in by_type.values():                       # other calls of the same type in the previous 60 min
        times = [c["ts"] for c in group]
        for i, c in enumerate(group):
            left = bisect.bisect_left(times, c["ts"] - 3600)
            right = bisect.bisect_right(times, c["ts"])
            c["city_recent_60m"] = right - left - 1
    keys = Counter()
    for c in calls:
        c["surge_level"] = surge_level(c["city_recent_60m"])
        c["text"] = (f"{c['call_label']} reported. Situation: {c['surge_level']}, "
                     f"{c['city_recent_60m']} similar calls city-wide during the previous hour.")
        key = (f"{c['created_at']:%Y-%m-%dT%H:%M:%S}|{c['call_type']}|{c['borough'] or ''}|"
               f"{pandas_float(c['lat'])}|{pandas_float(c['lon'])}")
        c["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nyc311-ida|{key}|{keys[key]}"))   # = Qdrant point id
        keys[key] += 1


# --- 3-4. windows, waves, co-surge ------------------------------------------------------------------


def windows_and_waves(calls: list[dict]) -> tuple[list[dict], list[dict], dict]:
    first = calls[0]["created_at"].replace(minute=0, second=0)
    last = calls[-1]["created_at"].replace(minute=0, second=0)
    hours = []
    t = first
    while t <= last:
        hours.append(t)
        t = (t.astimezone(dt.timezone.utc) + dt.timedelta(hours=1)).astimezone(TZ)
    counts = {h.strftime("%Y-%m-%dT%H"): Counter() for h in hours}
    for c in calls:
        counts[c["window"]][c["incident_type"]] += 1
    wins = []
    for h in hours:
        wid = h.strftime("%Y-%m-%dT%H")
        n = counts[wid]
        wins.append({"id": wid, "start": h, "n_flood": n["flood"], "n_signal": n["traffic_signal_failure"],
                     "n_total": sum(n.values()),
                     "phase": "pre_storm" if h < STORM_START else "storm_night" if h < STORM_END else "aftermath",
                     "episode": None})
    spans: list[list[int]] = []
    for i, w in enumerate(wins):
        if w["n_flood"] < EPISODE_MIN:
            continue
        if spans and i - spans[-1][1] - 1 <= MERGE_GAP_H:
            spans[-1][1] = i
        else:
            spans.append([i, i])
    waves = []
    for k, (a, b) in enumerate(spans, 1):
        span = wins[a:b + 1]
        for w in span:
            w["episode"] = f"E{k}"
        ids = {w["id"] for w in span}
        floods = [c for c in calls if c["window"] in ids and c["incident_type"] == "flood"]
        peak = max(span, key=lambda w: w["n_flood"])     # first maximum, like idxmax
        boroughs = Counter(c["borough"] for c in floods if c["borough"])
        top = min(boroughs.items(), key=lambda kv: (-kv[1], kv[0]))[0] if boroughs else None   # mode, ties sorted
        end = (span[-1]["start"].astimezone(dt.timezone.utc) + dt.timedelta(hours=1)).astimezone(TZ)
        waves.append({"id": f"E{k}", "start": span[0]["start"].isoformat(), "end": end.isoformat(),
                      "hours": b - a + 1, "peak_calls_per_h": peak["n_flood"], "peak_hour": peak["start"].isoformat(),
                      "flood_calls": len(floods), "top_borough": top})
    inside = [w["n_signal"] for w in wins if w["episode"]]
    outside = [w["n_signal"] for w in wins if not w["episode"]]
    rin, rout = statistics.fmean(inside), statistics.fmean(outside)
    cosurge = {"when": "flood", "also_rises": "traffic_signal_failure", "per_hour_in_waves": round(rin, 2),
               "per_hour_outside": round(rout, 2), "ratio": round(rin / rout, 1) if rout else None}
    return wins, waves, cosurge


# --- 5. the library read by the graph module -------------------------------------------------------


def library(calls: list[dict], wins: list[dict], waves: list[dict], cosurge: dict, source: Path) -> dict:
    in_wave = {w["id"] for w in wins if w["episode"]}
    types: dict[str, dict] = {}
    for itype in sorted({c["incident_type"] for c in calls}):
        sub = [c for c in calls if c["incident_type"] == itype]
        levels = {}
        for _, label in SURGE_LEVELS:
            lv = [c for c in sub if c["surge_level"] == label]
            if not lv:
                continue
            levels[label] = {"calls": len(lv),
                             "share_in_wave": round(sum(c["window"] in in_wave for c in lv) / len(lv), 2),
                             "median_similar_60m": int(statistics.median(c["city_recent_60m"] for c in lv)),
                             "storm_night_share": round(sum(c["phase"] == "storm_night" for c in lv) / len(lv), 2)}
        types[itype] = {"calls": len(sub), "call_types": dict(Counter(c["call_label"] for c in sub).most_common()),
                        "levels": levels}
    hours = [w["n_flood"] for w in wins if w["phase"] == "pre_storm"]
    return {
        "source": DS,
        "title": "NYC 311 calls, Hurricane Ida (31 Aug - 3 Sep 2021)",
        "built_from": source.name,
        "built_by": "brain/tools/build_history.py (port of brain/notebooks/nyc311_ida_*.ipynb)",
        "calls": len(calls),
        "period": [calls[0]["created_at"].isoformat(), calls[-1]["created_at"].isoformat()],
        "surge_levels": [{"max_similar_60m": top, "label": label} for top, label in SURGE_LEVELS],
        "wave_rule": {"busy_hour_min_flood_calls": EPISODE_MIN, "merge_gap_h": MERGE_GAP_H},
        "baseline": {"flood_calls_per_h_before_storm": round(statistics.fmean(hours), 1) if hours else None,
                     "flood_calls_per_h_peak": max(w["n_flood"] for w in wins)},
        "waves": waves,
        "co_surge": [cosurge],
        "types": types,
    }


# --- Neo4j export (the notebook's graph) ----------------------------------------------------------------


def cypher(calls: list[dict], wins: list[dict], waves: list[dict], cosurge: dict) -> str:
    def rows(items: list[dict]) -> str:
        return json_cypher(items)

    out = [f"// NYC 311 x Hurricane Ida: graph of the notebook nyc311_ida_neo4j_graph.ipynb.",
           f"// Generated by brain/tools/build_history.py. Every node is tagged ds = '{DS}';",
           f"// re-running it does not duplicate anything (MERGE). Remove it: MATCH (n {{ds: '{DS}'}}) DETACH DELETE n;"]
    for label, key in (("Call", "id"), ("CallType", "name"), ("IncidentType", "name"), ("Window", "id"),
                       ("Episode", "id"), ("Zone", "id"), ("Borough", "name")):
        out.append(f"CREATE CONSTRAINT {label.lower()}_{key} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE;")
    itypes = sorted({c["incident_type"] for c in calls})
    out.append(f"UNWIND {rows([{'name': i} for i in itypes])} AS r MERGE (i:IncidentType {{name: r.name}}) SET i.ds = '{DS}';")
    ctypes = {c["call_type"]: c for c in calls}
    out.append(f"UNWIND {rows([{'name': k, 'label': c['call_label'], 'group': c['call_group'], 'itype': c['incident_type']} for k, c in sorted(ctypes.items())])} AS r "
               f"MERGE (t:CallType {{name: r.name}}) SET t.label = r.label, t.call_group = r.group, t.ds = '{DS}' "
               f"WITH t, r MATCH (i:IncidentType {{name: r.itype}}) MERGE (t)-[:INSTANCE_OF]->(i);")
    boroughs = sorted({c["borough"] for c in calls if c["borough"]})
    out.append(f"UNWIND {rows([{'name': b} for b in boroughs])} AS r MERGE (b:Borough {{name: r.name}}) SET b.ds = '{DS}';")
    out.append(f"UNWIND {rows([{k: v for k, v in w.items() if k != 'start'} | {'start_ts': int(w['start'].timestamp())} for w in wins])} AS r "
               f"MERGE (w:Window {{id: r.id}}) SET w.n_flood = r.n_flood, w.n_signal = r.n_signal, w.n_total = r.n_total, "
               f"w.phase = r.phase, w.start_ts = r.start_ts, w.ds = '{DS}';")
    out.append(f"UNWIND {rows([{'a': wins[i]['id'], 'b': wins[i + 1]['id']} for i in range(len(wins) - 1)])} AS r "
               f"MATCH (a:Window {{id: r.a}}), (b:Window {{id: r.b}}) MERGE (a)-[:NEXT]->(b);")
    out.append(f"UNWIND {rows(waves)} AS r MERGE (e:Episode {{id: r.id}}) SET e += r, e.ds = '{DS}';")
    out.append(f"UNWIND {rows([{'a': w['id'], 'b': w['episode']} for w in wins if w['episode']])} AS r "
               f"MATCH (w:Window {{id: r.a}}), (e:Episode {{id: r.b}}) MERGE (w)-[:PART_OF]->(e);")
    zones: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        if c["zone"]:
            zones[c["zone"]].append(c)
    zrows = []
    for z, cs in sorted(zones.items()):
        bs = Counter(c["borough"] for c in cs if c["borough"])
        zrows.append({"id": z, "lat": round(statistics.fmean(c["lat"] for c in cs), 5),
                      "lon": round(statistics.fmean(c["lon"] for c in cs), 5),
                      "borough": min(bs.items(), key=lambda kv: (-kv[1], kv[0]))[0] if bs else None})
    out.append(f"UNWIND {rows(zrows)} AS r MERGE (z:Zone {{id: r.id}}) SET z.lat = r.lat, z.lon = r.lon, z.ds = '{DS}' "
               f"WITH z, r WHERE r.borough IS NOT NULL MATCH (b:Borough {{name: r.borough}}) MERGE (z)-[:IN_BOROUGH]->(b);")
    for i in range(0, len(calls), 1000):
        batch = [{"id": c["id"], "call_type": c["call_type"], "surge_level": c["surge_level"],
                  "city_recent_60m": c["city_recent_60m"], "ts": c["ts"], "phase": c["phase"], "has_geo": c["has_geo"],
                  "lat": c["lat"], "lon": c["lon"], "text": c["text"], "window": c["window"], "zone": c["zone"],
                  "borough": c["borough"]} for c in calls[i:i + 1000]]
        out.append(
            f"UNWIND {rows(batch)} AS r MERGE (c:Call {{id: r.id}}) "
            f"SET c.call_type = r.call_type, c.surge_level = r.surge_level, c.city_recent_60m = r.city_recent_60m, "
            f"c.ts = r.ts, c.phase = r.phase, c.has_geo = r.has_geo, c.lat = r.lat, c.lon = r.lon, c.text = r.text, c.ds = '{DS}' "
            f"WITH c, r MATCH (t:CallType {{name: r.call_type}}) MERGE (c)-[:OF_TYPE]->(t) "
            f"WITH c, r MATCH (w:Window {{id: r.window}}) MERGE (c)-[:IN_WINDOW]->(w) "
            f"WITH c, r CALL {{ WITH c, r WITH c, r WHERE r.zone IS NOT NULL MATCH (z:Zone {{id: r.zone}}) MERGE (c)-[:IN_ZONE]->(z) }} "
            f"WITH c, r CALL {{ WITH c, r WITH c, r WHERE r.borough IS NOT NULL MATCH (b:Borough {{name: r.borough}}) MERGE (c)-[:IN_BOROUGH]->(b) }};")
    out.append(f"MATCH (a:IncidentType {{name: '{cosurge['when']}'}}), (b:IncidentType {{name: '{cosurge['also_rises']}'}}) "
               f"MERGE (a)-[r:CO_SURGES_WITH]->(b) SET r.per_hour_in_waves = {cosurge['per_hour_in_waves']}, "
               f"r.per_hour_outside = {cosurge['per_hour_outside']}, r.ratio = {cosurge['ratio']};")
    return "\n".join(out) + "\n"


def json_cypher(value) -> str:
    """A Python value as a Cypher literal (map keys unquoted, strings JSON-escaped)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {json_cypher(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(json_cypher(v) for v in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True, type=Path, help="311_ida.csv (raw NYC Open Data extract)")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--cypher", action="store_true", help="also write the Neo4j load script")
    args = ap.parse_args()

    calls = load(args.csv)
    features(calls)
    wins, waves, cosurge = windows_and_waves(calls)
    lib = library(calls, wins, waves, cosurge, args.csv)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{DS}.json").write_text(json.dumps(lib, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(calls):,} calls, {len(wins)} hourly windows, {len(waves)} surge waves; "
          f"traffic signals x{cosurge['ratio']} inside flood waves -> {args.out / f'{DS}.json'}")
    for w in waves:
        print(f"  {w['id']}: {w['start'][5:16]} -> {w['end'][5:16]}, {w['hours']} h, peak {w['peak_calls_per_h']}/h, "
              f"{w['flood_calls']} flood calls, mostly {w['top_borough']}")
    print("  surge levels:", dict(Counter(c["surge_level"] for c in calls)))
    if args.cypher:
        path = args.out / f"{DS}.cypher"
        path.write_text(cypher(calls, wins, waves, cosurge), encoding="utf-8")
        print(f"  Neo4j script -> {path} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
