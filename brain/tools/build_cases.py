"""Build the Brain's case memory and the mock Twin's fire traces from the NIST smoke alarm tests.

    python tools/build_cases.py [--zip vector-storage/data/nist/nist_room_tests.zip] [--check]

Same cleaning, windows and features as vector-storage/notebooks/nist_smoke_alarms_qdrant_neo4j.ipynb
(sections 1-3), in the standard library only, so the running Brain needs no numpy, pandas or vector
database. Writes (paths relative to brain/):

    data/cases/nist_fire_windows.csv   one row per 60 s window of a lab test: source, class, state,
                                       the 7 raw features, holdout = test group 5 (kept out of the memory)
    mocks/twin/traces/<name>.csv       one held-out test per mock scenario, one row per second, as rises
                                       above the test's own baseline (the mock adds them to its calm values)

--check runs the notebook's held-out evaluation (k nearest neighbours of each group-5 window among
groups 1-4) and prints the accuracy; the notebook reports 98.7 %.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import statistics
import sys
import zipfile
from collections import Counter
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = BRAIN / "vector-storage" / "data" / "nist" / "nist_room_tests.zip"
CASES_OUT = BRAIN / "data" / "cases" / "nist_fire_windows.csv"
TRACES_OUT = BRAIN / "mocks" / "twin" / "traces"

SOURCES = {"FFoam": ("fire flaming", "fire"), "SFoam": ("fire smouldering", "fire"),
           "BHB": ("broiled burger", "nuisance"), "FryHB": ("fried burger", "nuisance"),
           "SF": ("stir-fry", "nuisance"), "Toast": ("toast", "nuisance")}
NAME_RE = re.compile(r"^(FFoam|SFoam|BHB|FryHB|SF|Toast)G(\d+)T(\d+)$")
HOLDOUT_GROUPS = {5}
WIN, STRIDE, K = 60, 15, 15
FEATURES = ["obsc", "obsc_slope", "co", "co_slope", "co2", "tc", "tc_slope"]
SCALES = [0.5, 0.005, 5.0, 0.05, 0.02, 2.0, 0.02]       # notebook section 3 (must match config/memory.yaml)
COLUMNS = {"Counter (s)": "t", "CO (ppm vol)": "co", "CO2 (vol %)": "co2", "TC #1 (deg C)": "tc1",
           "Obsc (%/ft) smoothed": "obsc", "Obsc (%/ft)": "obsc"}
# mock scenario -> (held-out test, first second replayed): the replay starts shortly before the test's
# smoke rises, so a scenario shows its signature within a minute
TRACES = {"fire": ("FFoamG5T1", None), "cooking": ("ToastG5T1", None)}
TRACE_LEAD_S = 20


def _num(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        return math.nan
    return v if math.isfinite(v) else math.nan


def load_test(raw: bytes) -> list[dict[str, float]]:
    """Rows sorted by t, duplicates of t dropped (first kept), columns t, obsc, co, co2, tc1 (NaN = missing)."""
    reader = csv.reader(io.StringIO(raw.decode("latin-1")), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        name = COLUMNS.get(h)
        if name and name not in idx:          # first of duplicated columns, as pandas' ~duplicated()
            idx[name] = i
    rows, seen = [], set()
    for rec in reader:
        r = {k: _num(rec[i]) if i < len(rec) else math.nan for k, i in idx.items()}
        if math.isnan(r["t"]) or r["t"] in seen:
            continue
        seen.add(r["t"])
        rows.append(r)
    rows.sort(key=lambda r: r["t"])
    return rows


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else math.nan


def baseline_of(rows: list[dict[str, float]]) -> dict[str, float]:
    """Smoke from the 60 s before ignition, gases and temperature from the first 10 s after it."""
    pre = [r["obsc"] for r in rows if r["t"] < 0 and not math.isnan(r["obsc"])]
    allo = [r["obsc"] for r in rows if not math.isnan(r["obsc"])]
    b = {"obsc": _median(pre) if pre else _median(allo[:5])}
    for c in ("co", "co2", "tc1"):
        b[c] = _median([r[c] for r in rows if r["t"] >= 0 and not math.isnan(r[c])][:10])
    return b


def window_features(win: list[dict[str, float]], b: dict[str, float]) -> dict[str, float] | None:
    """The 7 raw numbers of one window (rows at 1 s), or None with fewer than 20 rows.
    The same function runs live in src/memory/service.py (its own copy, on live readings)."""
    if len(win) < 20:
        return None

    def fin(v: float) -> float:
        return 0.0 if math.isnan(v) else float(v)

    def lvl(c: str) -> float:
        vals = [r[c] for r in win if not math.isnan(r[c])]
        return fin(sum(vals) / len(vals) - b[c]) if vals else 0.0

    def slope(c: str) -> float:
        v = [r[c] for r in win if not math.isnan(r[c])]
        if len(v) <= 10:
            return 0.0
        return fin((sum(v[-5:]) / 5 - sum(v[:5]) / 5) / max(len(v) - 5, 1))

    return {"obsc": max(lvl("obsc"), 0.0), "obsc_slope": slope("obsc"), "co": lvl("co"), "co_slope": slope("co"),
            "co2": lvl("co2"), "tc": lvl("tc1"), "tc_slope": slope("tc1")}


def encode(f: dict[str, float]) -> list[float]:
    """Signed log of each feature over its scale (notebook `encode`)."""
    return [math.copysign(math.log1p(abs(f[k]) / s), f[k]) for k, s in zip(FEATURES, SCALES, strict=True)]


def state_of(f: dict[str, float], cls: str) -> str:
    return "quiet" if f["obsc"] < 0.1 and f["co"] < 2 and f["tc"] < 2 else cls


def build(zip_path: Path) -> tuple[list[dict], dict[str, list[dict[str, float]]], dict[str, dict[str, float]]]:
    windows, tests, bases = [], {}, {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            stem = Path(name).stem
            m = NAME_RE.match(stem)
            if not m or not name.endswith(".tsv"):
                continue
            code, grp, trial = m.groups()
            rows = load_test(zf.read(name))
            b = baseline_of(rows)
            tests[stem], bases[stem] = rows, b
            source, cls = SOURCES[code]
            t_max = int(max(r["t"] for r in rows))
            for te in range(30, t_max + 1, STRIDE):
                win = [r for r in rows if te - WIN < r["t"] <= te and r["t"] >= 0]
                f = window_features(win, b)
                if f is None:
                    continue
                windows.append({"id": f"{stem}@{te}", "test": stem, "source": source, "class": cls,
                                "state": state_of(f, cls), "group": int(grp), "trial": int(trial), "t_s": te,
                                "holdout": int(int(grp) in HOLDOUT_GROUPS), **f})
    return windows, tests, bases


def knn_vote(q: list[float], db: list[tuple[list[float], str]], k: int = K) -> tuple[str, int]:
    nearest = sorted(db, key=lambda e: sum((a - b) ** 2 for a, b in zip(q, e[0], strict=True)))[:k]
    votes = Counter(s for _, s in nearest)
    order = {"quiet": 0, "nuisance": 1, "fire": 2}
    best = max(votes.items(), key=lambda kv: (kv[1], -order[kv[0]]))   # ties: as numpy argmax (lowest id)
    return best[0], best[1]


def write_cases(windows: list[dict]) -> None:
    CASES_OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "test", "source", "class", "state", "group", "trial", "t_s", "holdout", *FEATURES]
    with open(CASES_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in windows:
            w.writerow([r[c] if c not in FEATURES else f"{r[c]:.6g}" for c in cols])


def write_traces(tests: dict, bases: dict) -> dict[str, tuple[str, int]]:
    """Each trace: second, then the rise of smoke (%/ft), CO (ppm), CO2 (ppm) and ceiling temperature (C)
    above the test's baseline, from TRACE_LEAD_S before smoke first reaches 0.1 %/ft."""
    TRACES_OUT.mkdir(parents=True, exist_ok=True)
    out = {}
    for scenario, (test, start) in TRACES.items():
        rows, b = tests[test], bases[test]
        if start is None:
            first = next((r["t"] for r in rows if r["t"] >= 0 and not math.isnan(r["obsc"])
                          and r["obsc"] - b["obsc"] >= 0.1), 0.0)
            start = max(0, int(first) - TRACE_LEAD_S)
        last: dict[str, float] = {"obsc": 0.0, "co": 0.0, "co2": 0.0, "tc1": 0.0}
        with open(TRACES_OUT / f"{scenario}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["# NIST TN 1947 smoke alarm test " + test + f", from {start} s after ignition; "
                        "rises above the test's own baseline"])
            w.writerow(["t", "smoke", "co", "co2_ppm", "temperature"])
            for r in rows:
                if r["t"] < start:
                    continue
                for c in last:                       # a missing value repeats the previous one
                    if not math.isnan(r[c]) and not math.isnan(b[c]):
                        last[c] = r[c] - b[c]
                w.writerow([int(r["t"] - start), f"{max(last['obsc'], 0):.4f}", f"{last['co']:.3f}",
                            f"{last['co2'] * 10000:.1f}", f"{last['tc1']:.3f}"])
        out[scenario] = (test, start)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    ap.add_argument("--check", action="store_true", help="evaluate on the held-out group (notebook: 98.7 %%)")
    args = ap.parse_args(argv)
    if not args.zip.is_file():
        print(f"not found: {args.zip}", file=sys.stderr)
        return 2
    windows, tests, bases = build(args.zip)
    write_cases(windows)
    traces = write_traces(tests, bases)
    train = [w for w in windows if not w["holdout"]]
    held = [w for w in windows if w["holdout"]]
    print(f"{len(tests)} tests, {len(windows):,} windows | memory {len(train):,} | held out {len(held):,}")
    print(f"wrote {CASES_OUT.relative_to(BRAIN)}")
    for sc, (test, start) in traces.items():
        print(f"wrote {(TRACES_OUT / f'{sc}.csv').relative_to(BRAIN)}  ({test} from {start} s)")
    if args.check:
        db = [(encode(w), w["state"]) for w in train]
        ok = sum(knn_vote(encode(w), db)[0] == w["state"] for w in held)
        print(f"held-out accuracy: {ok / len(held) * 100:.1f} % ({ok}/{len(held)} windows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
