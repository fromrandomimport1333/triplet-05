"""How residents report a flood: the numbers behind config/detection.yaml calls.by_category.flood.

    python tools/calibrate_calls.py [--csv vector-storage/data/nyc311/nyc311_ida_clean.csv]

Reads the cleaned NYC 311 calls of Hurricane Ida (dataset study in vector-storage/, notebook
nyc311_ida_full.ipynb), standard library only. Per ~1 km area (`zone_id` of the clean file) it measures
the gap between flood calls and how many calls follow the first one within 15, 30 and 60 minutes,
on the storm night and over the whole period, and prints the call window and full count it suggests:
the shortest window in which at least half of the flooded areas get a second call, and full strength
at that second call. It also prints the share of traffic-signal failures that came from an area
flooded in the previous hour.

311 is the city's non-emergency line: emergency calls (fire, accident) come faster, so only the
flood setting is taken from it.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]
DEFAULT_CSV = BRAIN / "vector-storage" / "data" / "nyc311" / "nyc311_ida_clean.csv"
WINDOWS_MIN = (15, 30, 60)


def load(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def by_area(rows: list[dict[str, str]], phase: str | None = None) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["incident_type"] == "flood" and r["zone_id"] != "unknown" and (phase is None or r["phase"] == phase):
            out[r["zone_id"]].append(int(r["ts"]))
    for ts in out.values():
        ts.sort()
    return out


def measure(areas: dict[str, list[int]]) -> dict[str, object]:
    gaps = [b - a for ts in areas.values() for a, b in zip(ts, ts[1:], strict=False)]
    follow = {w: [sum(1 for t in ts if t - ts[0] <= w * 60) for ts in areas.values()] for w in WINDOWS_MIN}
    second = {w: sum(1 for n in v if n >= 2) / len(v) for w, v in follow.items()}
    third = {w: sum(1 for n in v if n >= 3) / len(v) for w, v in follow.items()}
    three_in_2min = sum(1 for ts in areas.values() for i in range(len(ts) - 2) if ts[i + 2] - ts[i] <= 120)
    return {"areas": len(areas), "median_gap_min": statistics.median(gaps) / 60 if gaps else None,
            "second": second, "third": third, "three_within_2_min": three_in_2min}


def signal_share(rows: list[dict[str, str]], areas: dict[str, list[int]]) -> float:
    sig = [r for r in rows if r["incident_type"] == "traffic_signal_failure" and r["zone_id"] != "unknown"]
    hit = sum(1 for r in sig if any(int(r["ts"]) - 3600 <= t <= int(r["ts"]) for t in areas.get(r["zone_id"], ())))
    return hit / len(sig) if sig else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = ap.parse_args(argv)
    if not args.csv.is_file():
        print(f"not found: {args.csv}", file=sys.stderr)
        return 2
    rows = load(args.csv)
    night = measure(by_area(rows, "storm_night"))
    every = measure(by_area(rows))
    for name, m in (("storm night", night), ("whole period", every)):
        print(f"{name}: {m['areas']} flooded areas, median gap {m['median_gap_min']:.0f} min between calls, "
              f"{m['three_within_2_min']} times 3 calls within 2 min")
        for w in WINDOWS_MIN:
            print(f"   within {w:2d} min of the first call: 2nd call in {m['second'][w]:.0%} of areas, "
                  f"3rd in {m['third'][w]:.0%}")
    window = next((w for w in WINDOWS_MIN if night["second"][w] >= 0.5), WINDOWS_MIN[-1])
    print(f"traffic-signal failures from an area flooded in the previous hour: "
          f"{signal_share(rows, by_area(rows)):.0%}")
    print(f"suggested: calls.by_category.flood = {{window_s: {window * 60}, full_count: 2}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
