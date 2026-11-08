Put report.pdf and pitch.pdf here.

# Documentation

| Folder / file | What it contains |
|---|---|
| [`report.pdf`](report.pdf) | **Phase 1 report** (parts 0–5), built by `python docs/report/build_report.py` from the Markdown below |
| [`pitch.pdf`](pitch.pdf) | **Pitch deck** (7 slides); editable source and speaker notes in [`pitch/`](pitch/) |
| [`demo-script.md`](demo-script.md) | CSTAM slot: readiness checklist, the four live scenarios and what to say, expected questions, recovery in 2 minutes |
| [`report/part2-city-brain.md`](report/part2-city-brain.md), [`report/part5-collaboration.md`](report/part5-collaboration.md) | Report parts 2 and 5 (part 0 is `../INTEGRATION.md`, part 4 is generated from the scenarios and their logs) |
| [`challenge/phase1-requirements.md`](challenge/phase1-requirements.md) | What the organizers require for Phase 1 (rules R1–R4, scoring, report, Test Book, demo). Summary of the official Phase 1 Specifications PDF. |
| [`team-plan/`](team-plan/) | The City Brain team's module plan: shared contracts (`00`) and one document per module (`01-input` … `06-mock`). Copied unchanged. |
| [`proposals/`](proposals/) | Earlier design documents: the Phase 0 proposal, the City Brain design brief, and an independent architecture review. Summaries, plus how each relates to the current plan. |
| [`repository/branches.md`](repository/branches.md) | Branches of this repository: what each contains and its state. |
| [`repository/implementation-review.md`](repository/implementation-review.md) | Review of each implementation (strengths, weaknesses, tests run, integration) and the recommended way to combine them. |
| [`datasets/`](datasets/) | Candidate public datasets for the incident reference library, and the assessment of each. |
| [`../brain/vector-storage/`](../brain/vector-storage/) | Dataset studies on the chosen datasets (NIST smoke alarms, NYC 311 during Hurricane Ida, UTD19 Torino traffic): cleaning, vectors, Qdrant + Neo4j notebooks run in Colab, results and limits. |
| [`report/`](report/) | Report sources: parts 2 and 5, one paragraph per module (what, how, why, limits), the PDF builder. |
| [`diagrams/`](diagrams/) | Diagrams for the report. |

## Reference files not in the repository yet

The team plan links to these files with `../<name>` from `team-plan/`, i.e. it expects them in
this `docs/` folder. They will be added; until then those links are broken:

| File | Referenced by |
|---|---|
| `city-brain-interface-spec.pdf` | `team-plan/00`, `01`, `05`, `06` (topics, envelope, HMAC, error codes, test vector) |
| `city-brain-inputs-outputs.md` | `team-plan/00` |
| `guardian-proposal-alignment.md` | `team-plan/00` (mTLS, Ed25519, trust score) |
| `city-brain-architecture.png` | `team-plan/00` (architecture figure) |

The organizer and proposal PDFs (Phase 1 Specifications, Phase 0 proposal, City Brain design
brief, dataset note) are deliberately **not** committed; `challenge/` and `proposals/` hold
summaries of them.
