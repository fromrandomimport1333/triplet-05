# Incident history: NYC 311 calls, Hurricane Ida (2021)

A real storm as reference data for the graph module (`src/graph/history.py`).

| File | What | In git |
|---|---|---|
| `source/311_ida.csv` | Raw extract from NYC Open Data, 311 Service Requests: 6,275 calls (sewer backup, street flooding, catch basin, water in basement, traffic signal controller), 31 Aug to 3 Sep 2021 | yes |
| `nyc311-ida.json` | The library the graph loads at start (3 KB): surge levels per incident type, the 3 surge waves, the co-surge flood → traffic signal failures | yes |
| `nyc311-ida.cypher` | The same data as a Neo4j graph (7,075 nodes, 25,661 relationships), with `--cypher` | no (2.5 MB, rebuilt in 2 s) |

Rebuild (from `brain/`):

```bash
python tools/build_history.py --csv data/history/source/311_ida.csv --cypher
```

`tools/build_history.py` is the port of the two notebooks in `brain/notebooks/`
(`nyc311_ida_preprocessing.ipynb` for Qdrant, `nyc311_ida_neo4j_graph.ipynb` for Neo4j) to plain
Python. Its numbers equal the notebooks' saved outputs (checked by `tests/graph/test_history.py`):
6,273 calls after cleaning, waves E1 (7 h, peak 659 flood calls/h), E2 (15 h, peak 311/h),
E3 (7 h, peak 101/h), traffic signal failures 15.93/h inside waves vs 2.96/h outside (x5.4). The
call ids are the notebook's Qdrant point ids.

## Load it in Neo4j (to show the graph)

```bash
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/citybrain123 neo4j:5-community
docker exec -i neo4j cypher-shell -u neo4j -p citybrain123 < data/history/nyc311-ida.cypher   # ~1 min
```

Open http://localhost:7474 and try:

```cypher
MATCH (e:Episode) RETURN e.id, e.start, e.hours, e.peak_calls_per_h, e.flood_calls ORDER BY e.id;
MATCH (a:IncidentType)-[r:CO_SURGES_WITH]->(b) RETURN a.name, b.name, r.ratio;
MATCH p = (c:Call)-[:IN_WINDOW]->(:Window)-[:PART_OF]->(:Episode {id: 'E1'}) RETURN p LIMIT 50;
```

## How the Brain uses it

At run time there is no database, vector store or network: the graph module loads the JSON and,
for a live incident whose type is mapped (`config/graph.yaml` `history.types`, today flood),
matches its severity to a surge level (`history.severity_level`: high -> heavy surge, ...). It
returns a short hint in `GET /context/{incident_id}` (`history.match`, `history.hint`) and the
details in `GET /history/{incident_id}`; `GET /history` serves the whole library.

The notebook's Qdrant search embeds a templated sentence (call type + surge level + count), so
its nearest neighbours are the calls of the same type and a close count: matching on type and
level gives the same answer offline. The LLM adds the hint to its prompt only with
`context.use_history: true` in `config/llm.yaml` (off until the prompt is re-benchmarked).

Limits: another city and another storm; one hazard (flood) and one co-surge; the mapping from
the Brain's severity to a surge level is a configured choice, not learned.
