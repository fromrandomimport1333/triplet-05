"""Extract the Torino rows from the UTD19 download (no awk needed, works on Windows).

Usage:   python extract_torino.py utd19_u.csv detectors_public.csv
Output:  torino.csv and torino_dets.csv, written next to this script (upload both to Colab, or put them on Google Drive).
"""
import sys
from pathlib import Path
import pandas as pd

if len(sys.argv) != 3:
    sys.exit("Usage: python extract_torino.py utd19_u.csv detectors_public.csv")
big, dets_file = Path(sys.argv[1]), Path(sys.argv[2])
out_dir = Path(__file__).resolve().parent

# 1) the 7 GB file, read in pieces: keep only the Torino rows
kept, first, total = 0, True, 0
with open(out_dir / "torino.csv", "w", newline="", encoding="utf-8") as f:
    for chunk in pd.read_csv(big, chunksize=1_000_000):
        total += len(chunk)
        city = chunk["city"].astype(str).str.strip().str.lower()
        t = chunk[city == "torino"]
        if len(t):
            t.to_csv(f, header=first, index=False)
            first, kept = False, kept + len(t)
        print(f"\rread {total:,} rows, kept {kept:,} Torino rows", end="", flush=True)
print()
if kept == 0:
    sys.exit("No Torino rows found: check the 'city' column of the big file (values seen are printed by: pandas.read_csv(big, nrows=5)).")

# 2) the detector table: keep the Torino detectors
torino = pd.read_csv(out_dir / "torino.csv", usecols=["detid"])
ids = set(torino.detid.unique())
dets = pd.read_csv(dets_file)
city_col = next((c for c in ("citycode", "city") if c in dets.columns), None)
if city_col:
    dets = dets[dets[city_col].astype(str).str.strip().str.lower() == "torino"]
else:
    dets = dets[dets.detid.isin(ids)]
dets = dets[dets.detid.isin(ids)]
dets.to_csv(out_dir / "torino_dets.csv", index=False)
print(f"torino.csv: {kept:,} rows, {len(ids)} detectors | torino_dets.csv: {len(dets)} detectors")
print("written to", out_dir)
