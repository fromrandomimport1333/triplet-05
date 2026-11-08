"""Prototype: FloodNet event summaries -> incident-reference case cards (JSONL).

    python floodnet_to_cards.py <folder with events_summary.csv and sensors.csv> <out.jsonl>

Card fields follow the schema proposed in ../assessment.md. Events of 4 in or more on a
non-tidal sensor are real floods; shallower events and tidally influenced sensors are
ambiguous (minor ponding, high tide).
"""
import csv, json, math, os, sys

src, out = sys.argv[1], sys.argv[2]


def read(name):
    with open(os.path.join(src, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fl(x):
    """Float, or None when missing (NaN is not valid JSON)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def fmt(v, spec):
    return "?" if v is None else format(v, spec)


def sev(d):
    return "critical" if d >= 24 else "high" if d >= 12 else "medium" if d >= 4 else "low"


sens = {r["Sensor ID"]: r for r in read("sensors.csv")}
cards = []
for r in read("events_summary.csv"):
    d = fl(r["max_depth_inches"])
    if d is None:
        continue
    s = sens.get(r["sensor_id"], {})
    tidal = s.get("Tidally Influenced", "Unknown")
    real = d >= 4 and tidal != "Yes"
    onset, drain, dur = fl(r["onset_time_mins"]), fl(r["drain_time_mins"]), fl(r["duration_mins"])
    text = (f"Street flooding at {s.get('Sensor Name', r['sensor_id'])} ({s.get('Borough', '?')}, tidal={tidal}). "
            f"Water depth rose from 0 to a peak of {d:.1f} in over {fmt(onset, '.0f')} min, "
            f"drained in {fmt(drain, '.0f')} min; total {fmt(dur, '.0f')} min, "
            f"{fmt(fl(r['duration_above_4_inches_mins']), '.0f')} min above 4 in.")
    cards.append({
        "case_id": f"floodnet:{r['sensor_id']}:{r['flood_start_time']}",
        "source": "NYC FloodNet aq7i-eu5q", "source_record_id": f"{r['sensor_id']}@{r['flood_start_time']}",
        "licence": "CC BY-NC-SA 4.0 (FloodNet) / NYC Open Data terms",
        "incident_type": "flood", "case_class": "real_incident" if real else "ambiguous", "fault_type": None,
        "severity": sev(d), "domains": ["water_level"], "start": r["flood_start_time"], "end": r["flood_end_time"],
        "duration_min": dur,
        "signals": {"water_level": {"peak_in": d, "rise_min": onset, "drain_min": drain}},
        "location": {"city": "New York", "borough": s.get("Borough")}, "context": {"tidal": tidal},
        "summary": text,
    })

with open(out, "w", encoding="utf-8") as f:
    for c in cards:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")
print(len(cards), "cards;", os.path.getsize(out), "bytes")
print(json.dumps(cards[0], indent=1) if cards else "no cards")
