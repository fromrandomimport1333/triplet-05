# Vector storage for the City Brain (Qdrant + Neo4j)

Three notebooks that take real public datasets, store them in **Qdrant** (similarity search) and **Neo4j** (explanations), and **combine both**: a new situation goes in, Qdrant finds the most similar past moments, Neo4j explains why they look alike and what happened around them.

You run them in **Google Colab** against **your own** Qdrant Cloud cluster and Neo4j Aura instance. The notebooks themselves don't run on this computer.

**In the running Brain:** the NIST study is live as the **case memory** (`src/memory/`, see `brain/README.md` → *Case memory*). `python tools/build_cases.py --check` rebuilds the same 4,998 cases from `data/nist/nist_room_tests.zip`, reproduces the 98.7 % held-out result, and writes the two held-out tests the mock Twin replays. The search runs offline, in memory, without Qdrant (scenario B14: burnt toast, no fire truck). The **NYC 311** study calibrates how long a resident's flood call counts in detection: 60 min, full at 2 callers. `python tools/calibrate_calls.py` measures it from `data/nyc311/nyc311_ida_clean.csv`.

## What we did

| Step | What happens |
|---|---|
| **Clean** | Each dataset is cleaned (timestamps, labels, impossible values, duplicates, missing positions). |
| **Turn into vectors** | NYC 311: a short sentence per call, embedded with a text model. NIST and UTD19: numbers computed from the sensor readings, relative to a baseline so another sensor or city can be encoded the same way. |
| **Qdrant** | One point per call / sensor window / detector-hour: a vector plus a small payload. Question answered: *"which past moments look like this one?"* |
| **Neo4j** | A graph with the context the vectors leave out: waves of the storm, which signal each fire source raises, which road, which neighbouring detectors. Question answered: *"why, and what else was going on?"* |
| **Combine** | The last section of every notebook: a new reading → Qdrant neighbours → Neo4j explanation. The two stores share one key, the point id (`point_id` in Qdrant = `id` of the `Call` / `Reading` node in Neo4j). |

## Folder contents

```
vector-storage/
├── README.md                              this file
├── notebooks/
│   ├── nyc311_ida_full.ipynb                   NYC 311 calls, Hurricane Ida  (starts from the clean file)
│   ├── nist_smoke_alarms_qdrant_neo4j.ipynb    NIST smoke alarms: fire or cooking?
│   └── utd19_torino_qdrant_neo4j.ipynb         UTD19 Torino traffic loop detectors
└── data/
    ├── nyc311/nyc311_ida_clean.csv        6,273 cleaned calls (input of the NYC notebook)
    ├── nist/nist_room_tests.zip           the 90 NIST room tests (input of the NIST notebook)
    └── utd19/extract_torino.py            script that builds the UTD19 input files (the data itself is NOT included, see below)
```

## The notebooks at a glance

| | NYC 311 (Ida) | NIST smoke alarms | UTD19 Torino traffic |
|---|---|---|---|
| **Notebook** | `nyc311_ida_full.ipynb` | `nist_smoke_alarms_qdrant_neo4j.ipynb` | `utd19_torino_qdrant_neo4j.ipynb` |
| **Input file(s)** | `nyc311_ida_clean.csv` | `nist_room_tests.zip` | `torino.csv` + `torino_dets.csv` (you build them, see below) |
| **Starts from** | the clean file (cleaning already done) | raw tests (cleaning is in the notebook) | raw Torino extract (cleaning is in the notebook) |
| **What one point is** | one 311 call | one 60-second window of a lab test | one detector-hour |
| **Vector** | 384-number text embedding (`all-MiniLM-L6-v2`), cosine | 7 numbers (smoke, CO, CO2, temperature level and trend), Euclidean | 6 numbers (speed, occupancy, flow vs the detector's own normal), Euclidean |
| **Qdrant collection** | `nyc311_calls` | `nist_fire_windows` | `utd19_torino_windows` |
| **Neo4j tag (`ds`)** | `nyc311-ida` | `nist-smoke` | `utd19-torino` |
| **Main graph idea** | surge **waves** of the storm | which **signal** each source raises (CO rises for fires, never for cooking) | jams seen by **several neighbouring detectors** vs one (real jam vs glitch) |
| **Shared node** | `IncidentType` `flood`, `traffic_signal_failure` | `IncidentType` `fire` | `IncidentType` `traffic_jam` |
| **Held out as "new inputs"** | n/a (new situations are typed in) | test group 5 (1,264 windows) | 8 % of the detectors |

## The datasets: what is in them and what they are for

All three are real public data, chosen because each one is a different kind of signal a smart-city "City Brain" would receive: **citizen calls**, **fire sensors** and **road sensors**.

### NYC 311 calls during Hurricane Ida (`data/nyc311/nyc311_ida_clean.csv`)

- **Content:** the calls New Yorkers made to the 311 non-emergency line between 31 Aug and 3 Sep 2021, around Hurricane Ida. 6,275 raw calls, 6,273 after cleaning. Each row is one call: when it was made, what it was about, which borough and ZIP, and the position. The calls are of 5 kinds: sewer backup (the most common, about 4,000), street flooding, clogged catch basin, water in a basement, and traffic-signal failure. The clean file adds: storm phase (before / during / after), a roughly 1 km zone, how many similar calls came in the previous hour, a severity level, the sentence that gets embedded, and the fixed `point_id`.
- **Used for:** seeing how citizens report an emergency. Calls surge to about 650 flood calls an hour at the peak, and traffic-signal failures rise at the same time. In the City Brain, calls are the "people noticed it" signal that confirms or contradicts the flood sensors. Here it is used to find past situations similar to a new one (Qdrant) and to explain them with the storm's waves and places (Neo4j).
- **Limit:** 311 is a non-emergency line and the free-text comments are not public, so only the category, time and place are available.

### NIST smoke alarm tests: real fires versus cooking (`data/nist/nist_room_tests.zip`)

- **Content:** 90 lab tests in a room, each recorded once per second from 60 seconds before ignition (about 101,000 seconds of data in total). Six kinds of source, 5 groups of 3 repeats each: two real fires (flaming foam, smouldering foam) and four cooking nuisances (broiled hamburger, fried hamburger, stir-fried vegetables, toast). Each second has the smoke level, CO, CO2, temperature (probe and four ceiling sensors), humidity, HCN, the smoke detector's ionisation current and light transmission. Tests last from 8 to 37 minutes.
- **Used for:** teaching a system to tell a **fire from burnt toast**. Smoke rises for every source, but CO rises only for the fires (a +10 ppm CO rise appears in all 30 fire tests and in none of the 60 cooking tests). Here it is used to recognise a new sensor reading by finding similar past moments (Qdrant) and to explain what each source normally does (Neo4j), and as a test bench for a fire-detection rule.
- **Limit:** indoor lab tests of one room and one material, with laboratory sensors; a cheap sensor in a real home will be noisier.

### UTD19 Torino traffic detectors (not included: build it with `data/utd19/extract_torino.py`)

- **Content:** readings from 399 loop detectors buried in the roads of Torino, every 5 minutes for 21 normal days (26 Sep to 16 Oct 2016), about 2 million rows: vehicles per hour (flow), occupancy (how much of the time a vehicle is over the sensor), speed, and an error flag. A second table gives each detector's road, road class, number of lanes, speed limit, road segment and GPS position. There are no labelled accidents or jams.
- **Used for:** learning what **normal traffic** looks like (two rush-hour peaks on weekdays, none on weekends) and how to spot a real jam without false alarms: a jam is a detector far below *its own* normal speed with high occupancy for 30 minutes, and it is only trusted when neighbouring detectors agree. Here it is used to find past hours that look like a new reading (Qdrant) and to say whether such hours were real, confirmed jams (Neo4j).
- **Limit:** one city, normal days only, and the data may be used for academic, non-commercial work only.

## Before you start

You need three free things:

1. **Google Colab** (colab.research.google.com) with a Google account.
2. **A Qdrant Cloud cluster** (cloud.qdrant.io): note the **cluster URL** (like `https://xxxx.region.cloud.qdrant.io:6333`) and create an **API key**.
3. **A Neo4j Aura instance** (console.neo4j.io): Aura gives you a credentials file with the **URI** (`neo4j+s://xxxx.databases.neo4j.io`), the **username**, the **password** (shown once: keep the file) and sometimes a **database name**.

### Put the credentials in Colab Secrets (never in a cell)

In Colab: left sidebar → key icon (**Secrets**) → add these, and switch on **Notebook access** for each:

| Secret name | Value |
|---|---|
| `QDRANT_URL` | your Qdrant cluster URL |
| `QDRANT_API_KEY` | your Qdrant API key |
| `NEO4J_URI` | `neo4j+s://xxxx.databases.neo4j.io` |
| `NEO4J_USERNAME` | usually `neo4j` |
| `NEO4J_PASSWORD` | your Aura password |
| `NEO4J_DATABASE` | *(optional)* only if your Aura database is not called `neo4j`. In the runs behind this README it was a short id (see the Aura console) |

If a secret is missing, the notebook asks for it when needed (passwords and keys are typed hidden). Nothing is stored in the notebook file.
A free Aura instance that has been idle may be **paused**: press resume in the Aura console before running the Neo4j sections.

## How to run a notebook (same for all three)

1. In Colab: **File → Upload notebook**, pick one file from `notebooks/`.
2. Add the secrets above (once; they are shared by all notebooks in your Colab account).
3. Run the cells **from the top**. The first sections work without any cloud account: loading, cleaning, charts and offline checks.
4. When a cell asks for the data, **upload the input file** in the dialog (see the table above). To avoid re-uploading every session, put the file in Google Drive: run `from google.colab import drive; drive.mount('/content/drive')` and `%cd /content/drive/MyDrive/<folder with the file>` in a first cell, then continue.
5. **Everything that writes to a cloud database is OFF by default.** Switch on, in this order, and re-run the cell that holds the switch:
   - `RUN_QDRANT = True` → embeds and uploads to Qdrant
   - `RUN_NEO4J = True` → loads the graph into Neo4j and runs the Cypher questions
   - `RUN_COMBINE = True` → the final "Qdrant finds similar, Neo4j explains" section (needs both uploads done)
6. Re-running is safe: points and nodes have fixed ids and are merged, not duplicated.

Where the switches are: **NYC**: `RUN_NEO4J`, `RUN_COMBINE`, `CLEAR_GRAPH` in the first constants cell (section 0); `RUN_QDRANT` and `RECREATE` in the section 4 cell. **NIST and UTD19**: all of them together in the section 0 switches cell.

### NYC 311: `nyc311_ida_full.ipynb`

Sections: 0 Setup · 1 Load the clean dataset · 2 Charts · 3 Qdrant records · 4 Qdrant upload · 5 Graph tables · 6 Connect to Neo4j · 7 Load graph · 8 Cypher questions · 9 Explain one call · 10 Combine.

1. Upload `data/nyc311/nyc311_ida_clean.csv` when section 1 asks.
2. Run sections 0–3 (no cloud needed). Set `RUN_QDRANT = True` and run section 4 (installs `sentence-transformers`, takes about a minute).
3. Set `RUN_NEO4J = True` and run sections 5–9, then `RUN_COMBINE = True` for section 10.

The vector holds only *incident type + severity* (for example "Sewer backup reported. Situation: heavy surge, 100 similar calls city-wide during the previous hour."), so it stays general. Place and time live in the graph (and, optionally, in the Qdrant payload with `INCLUDE_CONTEXT = True`).

How the clean file was made (this step is not part of this notebook): from the raw NYC 311 extract (`city-brain-notebooks/data/calls/nyc-311/311_ida.csv`) timestamps were put in New York time, the 5 descriptors mapped to 5 call types, coordinates and ZIP validated, 2 exact duplicates dropped, the borough inferred for 2 calls, and these columns added: storm phase, about 1 km zone, calls in the previous hour (city / borough / zone), severity level, the embedded `text` and the fixed `point_id`.

### NIST smoke alarms: `nist_smoke_alarms_qdrant_neo4j.ipynb`

Sections: 0 Setup · 1 Load · 2 Cleaning and charts · 3 Features · 4 Offline check · 5 Qdrant · 6 Neo4j · 7 Combine.

1. Upload `data/nist/nist_room_tests.zip` when section 1 asks (it unzips itself).
2. Sections 1–4 need no cloud account. They also show how well nearest-neighbour search separates fire from cooking on groups it never saw.
3. `RUN_QDRANT = True` (section 5), `RUN_NEO4J = True` (section 6), `RUN_COMBINE = True` (section 7). Test group 5 is kept out of both databases and used as "new readings".
4. To try your own sensor: build a table with columns `t, obsc, co, co2, tc1` at 1 reading per second and call `diagnose(your_table, t_end)` in section 7.

### UTD19 Torino traffic: `utd19_torino_qdrant_neo4j.ipynb`

**The data is not included**: UTD19 is free but only for academic / non-commercial use and you must sign up.

1. Sign up at https://utd19.ethz.ch. The download link comes **by email**. If nothing arrives, check spam, try again, or use the contact form on the site or the GitHub issues page github.com/ambuehll/UTD19/issues.
2. Download `utd19_u.csv` (about 7 GB, all cities) and `detectors_public.csv`.
3. On your computer run, from the folder where you want the output:
   ```
   python data/utd19/extract_torino.py utd19_u.csv detectors_public.csv
   ```
   It writes `torino.csv` (about 2 million rows, about 110 MB) and `torino_dets.csv` next to the script.
4. In Colab, upload both files when section 1 asks, or put them in Google Drive and set `DATA_DIR = "/content/drive/MyDrive/<folder>"`. Drive is much faster for the big file.
5. To try quickly first, set `MAX_DAYS = 7` and/or `MAX_DETECTORS = 100` in the switches cell.
6. Then `RUN_QDRANT`, `RUN_NEO4J`, `RUN_COMBINE` as above. About 8 % of the detectors are kept out as "new sensors".

## What ends up in the cloud

**Qdrant** (3 collections, one per dataset): the vector + a small payload per point (state / type / severity and a few readings). No credentials or personal data. NYC: 6,273 points. NIST: 4,998 points. UTD19: one point per sampled detector-hour (capped per state, up to roughly 15,000, so a free cluster is not filled with quiet hours).

**Neo4j** (one database can hold all three graphs):

| Notebook | Nodes | Relationships (main ones) |
|---|---|---|
| NYC | `Call`, `CallType`, `IncidentType`, `Window`, `Episode`, `Zone`, `Borough` (7,075 nodes, 25,661 relationships) | `OF_TYPE`, `INSTANCE_OF`, `IN_WINDOW`, `NEXT`, `PART_OF`, `IN_ZONE`, `IN_BOROUGH`, `CO_SURGES_WITH` |
| NIST | `Source`, `Signal`, `Test`, `Reading`, `IncidentType` (5,081 nodes, 10,149 relationships) | `OF_SOURCE`, `FROM_TEST`, `NEXT`, `RAISED`, `RAISES`, `INDICATES`, `MIMICS` |
| UTD19 | `Detector`, `Link`, `Road`, `JamEpisode`, `Incident`, `Reading`, `IncidentType` | `ON_LINK`, `ON_ROAD`, `NEXT_TO`, `AT`, `PART_OF`, `IS_A`, `IN_EPISODE` |

`IncidentType` nodes are **shared**: the three graphs connect through them (`flood`, `traffic_signal_failure`, `fire`, `traffic_jam`), which is where the City Brain can later link datasets. Every other node carries a tag `ds` with the dataset name above.

## Safe by default

- All uploads are off until you switch them on.
- An existing Qdrant collection is **kept** and upserted into. `RECREATE = True` deletes it first (destroys the points already in it).
- Neo4j loads use `MERGE`: no duplicates. `CLEAR_GRAPH = True` deletes **only** nodes tagged with that notebook's `ds`, never the rest of your database.
- To remove everything of one notebook: set `CLEAR_GRAPH = True` and re-run its load cell; delete its Qdrant collection in the Qdrant console.

## What we saw when we ran them

From the notebooks saved after the Colab runs on real Qdrant Cloud and Neo4j Aura (no errors in either):

- **NIST**: leave-one-group-out accuracy 97.2 % over all windows; 2 of 60 cooking tests falsely flagged as fire. On the held-out test group, 98.7 % through Qdrant, identical to the offline search (100 % of windows agree). 4,998 points uploaded; graph loaded and verified (5,081 nodes, 10,149 relationships). Known hard case: smouldering foam just starting to smoke, before CO rises, looks like cooking.
- **NYC 311**: graph loaded and verified in Neo4j (7,075 nodes, 25,661 relationships); the combine section ran. The storm comes in 3 waves (peaks of 659, 311 and 101 flood calls per hour). Traffic-signal failures run about 5.4 times higher while a flood wave is on.
- **UTD19**: **not yet run on the real data** (waiting for the download link). It was tested end to end on synthetic data shaped like the Torino files (2.4 million rows, about 36 seconds), so the logic works, but the numbers on real data are still unknown.

## Troubleshooting

| Message / symptom | What to do |
|---|---|
| `Could not reach Qdrant at ...` | URL must start with `https://` and usually end with `:6333`; check the API key and that the cluster is running. |
| `Could not connect to Neo4j ...` | Resume the Aura instance if paused; URI must start with `neo4j+s://`; re-check username and password; set `NEO4J_DATABASE` if your database is not `neo4j`. |
| Secrets are not picked up | In Colab Secrets switch on **Notebook access** for each secret; otherwise the notebook asks you to type them. |
| `Collection ... has N dims but ...` | The collection exists with another vector size. Use another collection name or set `RECREATE = True`. |
| Neo4j count check fails after loading | Set `CLEAR_GRAPH = True` and run the load again, then check the Aura instance is not out of room (free instances have node limits). |
| Colab restarted | Run the cells from the top again; the switches go back to `False`. |
| UTD19 upload is slow | Put `torino.csv` in Google Drive and set `DATA_DIR`. |

## Datasets and licences

| Dataset | Source | Licence |
|---|---|---|
| NYC 311 service requests | NYC Open Data, dataset `erm2-nwe9`, 31 Aug – 3 Sep 2021 | open |
| NIST smoke alarm tests | NIST, *Data on smoke alarm performance: a compilation to NIST TN 1947*, DOI 10.18434/T4530X | US government work, public domain (cite NIST TN 1947) |
| UTD19 | ETH Zurich, https://utd19.ethz.ch, Loder et al. (2019), *Scientific Reports* 9 | free after sign-up, **academic / non-commercial only**: do not publish or commit the files |

## Limits to keep in mind

- NIST is 90 lab tests of one room and one material, repeated three times per setup: it proves the method, not how a cheap sensor in a real flat will behave.
- UTD19 has no labelled incidents. "Jam" is a rule and "confirmed" only means several neighbouring detectors agreed.
- NYC 311 vectors know only incident type and severity, so many calls get near-identical vectors; that is intended for finding "a situation like this one".
- Everything here comes from single events or short periods in one city. Applying it to another city needs relative measures (baselines per sensor, severity relative to normal volume) and a check on that city's data first.
