# Dataset assessment for the incident reference library

Checked on 20 Oct 2026 by reading each dataset's pages and data dictionaries, and by
downloading small samples where access was open (no accounts were created). Items marked
*not verified* could not be checked (login wall or site unreachable).

## Short answer

None of the four datasets in the note works as a reference library if loaded raw:

- two are behind a login (UTD19, SmartGrid);
- the "Fire and Smoke Digital Twin" data is a thin dispatch feed with no sensor data;
- FARS covers fatal crashes only.

**NYC FloodNet fits well.** The recommendation is about **1,300 derived case cards (≈ 3–5 MB
with embeddings)**, built mostly from **NYC Open Data**. It is open, needs no login, and can be
queried with SoQL, so you pull exactly the small subset you need. FARS and raw traffic time
series are better used **offline, to calibrate detection thresholds**.

## The datasets of the note

| Dataset | Access | Size / format | Maps to | Verdict |
|---|---|---|---|---|
| **FARS 2024** (NHTSA) | Open download (`static.nhtsa.gov/nhtsa/downloads/FARS/{YEAR}/National/FARS{YEAR}NationalCSV.zip`) | ~33 MB zip/year; `accident.csv` 23 MB, 36,297 crashes, 80 columns | accident, **indirect** (no sensor data, no free text) | Use a small subset |
| **UTD19** (ETH Zurich, utd19.ethz.ch) | **Sign-up + accept conditions** (not done) | 6.51 GB CSV, 169M rows, 40 cities, 23,541 detectors, 2017–2019, 3–5 min | traffic speed / flow / occupancy, **direct**; **no incident labels** | Offline calibration only, if someone signs up; otherwise skip |
| **"Fire and Smoke Digital Twin"**: most likely Jiao et al. 2023 (arXiv 2305.18313), data `github.com/urbaninfolab/FireIncidentData` | Open (GitHub raw) | small JSON per city per day | fire dispatch titles only, **no sensor data** | **Skip** (see quality issues); use FDNY dispatch instead |
| **NYC FloodNet**: events `aq7i-eu5q`, sensors `kb2e-tjy3` (NYC Open Data); GitHub `floodnet-nyc/floodnet-data` | Open (SODA API / CSV); GitHub data **CC BY-NC-SA 4.0** (attribution required, non-commercial OK) | 3,526 QC'd events, 319 sensors, Nov 2020 – Oct 2026; summary ≈ 435 KB | water level, **direct**; flood, **direct** | **Use** |
| **SmartGrid-CyberPhysical-Attack-Dataset** (IEEE DataPort, DOI 10.21227/symr-bz19) | **Login + DataPort subscription** (free for IEEE Society members) | 4.63 MB zip: Benign / Backdoor / Bruteforce / FDI / Ransomware / Reverseshell, each `cyber.csv` + `physical.csv`; columns *not verified* | power, **indirect**: mostly IT attacks, no natural faults or outages | Skip, or only FDI + Benign if a Society member downloads it |

**Details that matter**

- **FARS.**
  - Useful fields: lat/lon, hour/minute, road class (`FUNC_SYSNAME`), harmful event, collision manner, weather, light condition, notification and arrival times, fatalities. 773 of the 2024 crashes are in urban New York.
  - Fatal crashes only, so it is biased toward severe and rural crashes. The CrashAPI was blocked (HTTP 403).
  - Value: priors such as hour-of-day accident risk. Not similarity search.
- **UTD19.**
  - `UTD19.csv` has city, detector id, day, interval (seconds from midnight), flow (veh/h/lane), occupancy, speed (km/h) and an error flag. `detectors.csv` has position, road class, speed limit and lanes.
  - Useful for rush-hour vs off-peak envelopes (fundamental diagram).
  - Licence text *not verified*.
- **FireIncidentData.**
  - Fields: title, lat/lon, description (address, agency, time), date, active status.
  - Problems found in samples:
    - the Seattle folder contains San Diego data;
    - every timestamp is labelled CDT;
    - Austin stops in Jan 2023;
    - some days return 404;
    - non-fire types are mixed in;
    - the repo has no licence.
- **FloodNet.**
  - Per event: start/end, max depth, onset and drain times, durations above 4/12/24 in, and a depth profile (≈ 60 s steps, gaps up to 240 s).
  - Depth: median peak 3.5 in, p90 11.1 in, max 46.1 in. 1,571 events reach ≥ 4 in and 291 reach ≥ 12 in.
  - **67 tidally influenced sensors** flood on high tide: a natural "ambiguous, not a rain flood" class.
  - Limits: depth only (no rainfall), and only events of 10 mm or more, so there are no "dry" windows.

## Better-fitting alternatives (all open, NYC Open Data unless stated)

| Source | Size | Gives | Verdict |
|---|---|---|---|
| **FDNY Fire Incident Dispatch** `8m42-w767` | 12.17M rows, 2005 – Sep 2026 | structural fires, vehicle accidents, utility water / electric emergencies, manhole fires, and **false alarms** ("Alarm System – Unnecessary": 34,370 in 2024): real false-positive analogues. No free text | **Use subset** |
| **NYC 311** `erm2-nwe9` | 22.85M rows | street flooding, congestion/gridlock, traffic-signal controller faults, "hitting power lines"; outcome text "could not find the problem" gives **ambiguous** reports | **Use subset** |
| **NYC DOT Traffic Speeds** `i4gi-tjb9` | 5-min per link | traffic speed, **direct**; includes **real dropouts** (status −101, speed 0) and **frozen runs**: real sensor faults | **Use subset** |
| **NYC Motor Vehicle Collisions** `h9gi-nx95` | 2.27M rows, 2012 – Jun 2026 | accidents incl. non-fatal, joinable with DOT speeds | Use subset (better than FARS for NYC) |
| **OpenAQ archive** (S3 `openaq-data-archive`) | ~1 KB per location-day | air quality (PM2.5 …), fire/smoke indirect; the v3 API needs a key | Small subset; licence varies by provider |
| **MSU/ORNL Power System Attack datasets** | *not verified* | PMU data with natural faults, maintenance and data-injection attacks | Recommended replacement for SmartGrid (check its README) |

*Not verified (sites unreachable):* DOE OE-417 outage summaries, NYISO 5-min zonal load. Not checked: CityLearn, Pecan Street (registration).

## Is a vector database the right store?

**Not for raw time series.** Embedding 5-minute speed or depth arrays gives poor nearest
neighbours, and similarity search is the wrong tool for detection. Use three layers instead:

1. **Thresholds and envelopes, computed offline.** Examples: hourly speed percentiles per link, flood depth p90, false-alarm rates. These drive detection (`config/detection.yaml`).
2. **Case cards in the vector DB.** Each is a short text summary plus a small feature vector (peak, rise rate, duration, z-score vs time-of-day baseline, cross-domain flags). Retrieve them by text embedding, then filter on metadata. They give precedents and explanations ("similar to the 30 Oct 2025 Queens flood").
3. **Fault cards built by injecting faults** (frozen, spike, drift, dropped, replayed) into real normal windows. No public dataset labels all five.

You need **hundreds of cards per type, not millions**. Recall depends on diversity, so
stratify each type by severity, time of day and context.

## Recommended subsets (2–3 days of work)

| # | Source | Filter | Cards |
|---|---|---|---|
| 1 | FloodNet `aq7i-eu5q` | stratified 200 events ≥ 4 in; 60 events < 4 in (minor ponding); 40 tidal-sensor events (ambiguous); ~20 multi-sensor storm-day cards (e.g. 30 Oct 2025: 128 events; 20 May 2026: 105) | ~320 |
| 2 | FDNY `8m42-w767` | 2024–2025: fires 120, vehicle accidents 60, utility water 40, utility electric 50, manhole fires 30, unnecessary / defective alarms 60 | ~360 |
| 3 | 311 `erm2-nwe9` | 2025: street flooding 60 (20 "could not find" = ambiguous), congestion 60, signal controller 30, power lines 30 | ~180 |
| 4 | DOT `i4gi-tjb9` (+ collisions `h9gi-nx95`) | 10 links × 4 weeks: rush-hour normal 60, unusual slowdowns 40, real dropouts / frozen 40; crashes within 500 m / 30 min of a slowdown 40 | ~180 |
| 5 | Synthetic faults | 5 fault types × 4 sensor types × 6 variants, injected into real windows from 1, 4 and OpenAQ | ~120 |
| 6 | Cross-domain | same storm day / borough: flood + 311 flooding + DOT slowdown; fire + PM2.5 rise; storm + utility electric | ~60 |
| 7 | FARS 2024 | 100 urban crashes stratified by weather × light × road class | ~100 |

**Total ≈ 1,300 cards**, at ~1 KB of JSON plus a 384-dimension float32 embedding (1.5 KB) each:
**about 3–5 MB**. A FloodNet prototype with all 3,526 events came to 2.5 MB of JSONL before embedding.

## Case-card schema (proposal)

| Field | Content |
|---|---|
| `case_id`, `source`, `source_record_id`, `licence` | provenance |
| `incident_type` | `flood`, `fire`, `accident`, `traffic_jam`, `power_problem`, `none` |
| `case_class` | `real_incident`, `normal`, `sensor_fault`, `ambiguous`, `cross_domain` |
| `fault_type` | `frozen`, `spike`, `drift`, `dropped`, `replayed`, or null |
| `severity`, `start`, `end`, `duration_min` | |
| `location` | city, borough, lat, lon, zone type |
| `time_context` | hour, weekday, rush hour, season; `weather` |
| `domains[]`, `signals` | per sensor: baseline, peak, delta, z-score, rise rate, recovery |
| `call_evidence` | category, count, example text |
| `expected_response` / `outcome`, `label_confidence` | |
| `summary` | the text that is embedded |

**Pipeline:**
1. SoQL pulls with filters and limits.
2. Normalise to UTC, local zones and canonical units (in → cm, mph → km/h).
3. Window and extract features against hour-of-week baselines.
4. Map source categories to `incident_type` / `case_class`.
5. Stratified sampling.
6. Fault injection.
7. Cross-source joins by borough and time.
8. Template-generated `summary`.
9. Embed with a small sentence-transformer and upsert with metadata.
10. **Hold out 20 % of the cards** to measure false-alarm and miss rates.

## Samples and scripts

The data samples used for this assessment (FloodNet events and a
case-card prototype, FDNY / 311 / DOT / collisions samples, an OpenAQ day, FARS extracts, the
UTD19 manual) are outside the repository, in the shared project folder `dataset-research/`.
The data is not committed; the scripts are in [`scripts/`](scripts/).
