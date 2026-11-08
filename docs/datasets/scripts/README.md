# Dataset scripts

Small scripts used for the dataset assessment (`../assessment.md`). They use only the Python
standard library. No data is committed: download it from the sources listed in the
assessment.

| Script | Run | Does |
|---|---|---|
| `floodnet_to_cards.py` | `python floodnet_to_cards.py <floodnet folder> <out.jsonl>` | Turns FloodNet event summaries into case cards in the schema of the assessment. **Prototype** |
| `floodnet_stats.py` | `python floodnet_stats.py <floodnet folder>` | Depth and duration percentiles, top flood days, tidal sensors, profile sampling gaps |
| `fars_stats.py` | `python fars_stats.py <accident.csv>` | Column list and counts by state, urban/rural, road class, collision manner, weather, light |
| `zip_remote.py` | `python zip_remote.py <url> [member] [out]` | Lists a remote zip and extracts one member with HTTP Range requests, without downloading the whole archive (FARS zips are ~33 MB) |
| `partial_zip.py` | `python partial_zip.py <truncated.zip> <out dir>` | Recovers the members present in a partly downloaded zip |

**Inputs.**
- The FloodNet folder holds `events_summary.csv` (NYC Open Data `aq7i-eu5q`) and `sensors.csv` (`kb2e-tjy3`). `floodnet_stats.py` also needs `events_profiles_sample.json`, a few events with their depth profiles.
- The NYC Open Data queries of the assessment (FDNY, 311, DOT speeds, collisions) were run ad hoc against the SODA API; there are no scripts for them yet.

**Choices in `floodnet_to_cards.py` to review.**
- An event is a real flood when its peak reaches 4 in or more on a sensor that is not tidally influenced; everything else is `case_class: ambiguous`.
- On the full data set (3,526 events) this gives 357 real-incident cards and 1,195 ambiguous cards at 4 in or more, because many deep floods are at tidal sensors. Storm surge at a coastal sensor *is* a real flood, so the tidal rule probably needs refining, for example using event duration or the rain day.
- Severity bands: 4 / 12 / 24 in.

**Licences.**
- FloodNet data is CC BY-NC-SA 4.0: attribution required, non-commercial use.
- NYC Open Data has its own terms of use.
- Keep the `licence` field of each card.
