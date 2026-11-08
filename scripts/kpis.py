"""KPIs of the Phase 0 report (Table 9) computed from the scenario summaries in logs/ (the .json next to each .log).

    python scripts\\kpis.py                 latest run of every scenario, Markdown table on stdout
    python scripts\\kpis.py --all           every run found
    python scripts\\kpis.py --out FILE      also write the table to FILE

Definitions (readings are judged against the Guardian's verdict timeline, see run_scenario.Context.summary):
  false-data recall     readings published while their sensor had an injected fault or a scenario attack, that the
                        Guardian had marked suspicious/untrusted at that moment / all such readings
  false alarms          healthy readings whose sensor was marked UNTRUSTED at that moment / all healthy readings
                        (the 15 s after a fault ends, quarantined readings and T6's Guardian-down window are not counted)
  time to flag          first non-trusted verdict on the faulty sensor - start of the fault or attack
                        (sensor faults only; actuator faults and attacks made while the Guardian was down are excluded)
  time to action        first command the Brain sent (not on false data) - the 'incident' mark of the scenario
                        (T2/T7: the TRUE water level crosses the Brain's threshold; T6: the jam condition starts, so it
                        includes the Brain's jam_sustain_s rule); also given from the Brain's own detection decision
  actions on false data commands sent while one of the sensors they relied on had a fault or an attack
"""
import argparse
import glob
import json
import os
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = {"recall": ">= 95%", "false_alarms": "<= 2%", "flag": "<= 10 s", "action": "<= 5 s", "false_actions": "0"}


def load(log_dir, every):
    runs = {}
    for p in sorted(glob.glob(os.path.join(log_dir, "t*-*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        s["_file"] = os.path.relpath(p, ROOT)
        if every:
            runs[s["_file"]] = s
        else:
            runs[s["scenario"]] = s                 # files sort by time, so the latest wins
    return sorted(runs.values(), key=lambda s: int(s["scenario"][1:]))


def pct(a, b):
    return "%.1f%% (%d/%d)" % (100.0 * a / b, a, b) if b else "n/a"


def per_run(s):
    r = s["readings"]
    down = (s["marks"].get("guardian_killed"), s["marks"].get("guardian_back") or s["end"])
    attacks = [a for a in s.get("attacks", []) if isinstance(a, dict)]      # single forged / replayed messages
    flags = [f.get("flagged_after_s") for f in s["faults"] + attacks
             if f.get("kind") != "actuator_stuck" and not (down[0] and down[0] <= f["start"] <= down[1])]
    missed = sum(1 for x in flags if x is None)
    flags = [x for x in flags if x is not None]
    sent = [d for d in s["decisions"] if d["event"] == "command_sent"]
    good = [d for d in sent if not d.get("on_false_data")]
    inc = s["marks"].get("incident")
    action = round(min(d["t"] for d in good if d["t"] >= inc) - inc, 1) if inc and [d for d in good if d["t"] >= inc] else None
    det = [d["t"] for d in s["decisions"] if d["event"] in ("flood_detected", "jam_detected")]
    after_det = round(min(d["t"] for d in good if d["t"] >= det[0]) - det[0], 1) if det and [d for d in good if d["t"] >= det[0]] else None
    return {"faulty": r["faulty"], "flagged": r["faulty_flagged"], "healthy": r["healthy"], "untrusted": r["healthy_untrusted"],
            "flags": flags, "missed": missed, "action": action, "after_detection": after_det,
            "false_actions": sum(1 for d in sent if d.get("on_false_data"))}


def table(runs):
    rows = ["| Scenario | Result | Faulty readings flagged | Healthy readings untrusted | Time to flag (s) | "
            "Incident → action (s) | Detection → action (s) | Actions on false data |",
            "|---|---|---|---|---|---|---|---|"]
    tot = {"faulty": 0, "flagged": 0, "healthy": 0, "untrusted": 0, "flags": [], "missed": 0, "false_actions": 0,
           "action": [], "after_detection": []}
    for s in runs:
        k = per_run(s)
        for key in ("faulty", "flagged", "healthy", "untrusted", "missed", "false_actions"):
            tot[key] += k[key]
        tot["flags"] += k["flags"]
        for key in ("action", "after_detection"):
            if k[key] is not None:
                tot[key].append(k[key])
        rows.append("| %s | %s | %s | %s | %s | %s | %s | %d |" % (
            s["scenario"].upper(), "PASS" if s["passed"] else "FAIL",
            pct(k["flagged"], k["faulty"]), pct(k["untrusted"], k["healthy"]),
            (", ".join("%.1f" % x for x in k["flags"]) or "-") + (" (+%d never)" % k["missed"] if k["missed"] else ""),
            k["action"] if k["action"] is not None else "-", k["after_detection"] if k["after_detection"] is not None else "-",
            k["false_actions"]))
    f = tot["flags"]
    summary = [
        "| KPI (Phase 0 report, Table 9) | Target | Measured on these runs |", "|---|---|---|",
        "| False data detected (recall, per reading) | %s | %s |" % (TARGETS["recall"], pct(tot["flagged"], tot["faulty"])),
        "| False alarms on healthy data (per reading) | %s | %s |" % (TARGETS["false_alarms"], pct(tot["untrusted"], tot["healthy"])),
        "| Time to flag a fault | %s | median %s s, max %s s over %d faults%s |" % (
            TARGETS["flag"], "%.1f" % statistics.median(f) if f else "-", "%.1f" % max(f) if f else "-", len(f),
            ", %d never flagged" % tot["missed"] if tot["missed"] else ""),
        "| Time from real incident to action | %s | %s |" % (TARGETS["action"], "; ".join(
            "%s: %s s" % (s["scenario"].upper(), per_run(s)["action"]) for s in runs if per_run(s)["action"] is not None) or "-"),
        "| Time from the Brain's detection to action | (not in Table 9) | %s |" % (
            "max %.1f s" % max(tot["after_detection"]) if tot["after_detection"] else "-"),
        "| Actions taken on false data | %s | %d |" % (TARGETS["false_actions"], tot["false_actions"]),
    ]
    return "\n".join(summary + [""] + rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=os.path.join(ROOT, "logs"))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    runs = load(a.log_dir, a.all)
    if not runs:
        print("no scenario summaries (logs/t*-*.json) found: run python scenarios\\run_scenario.py all first")
        return 1
    out = table(runs)
    print(out)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
