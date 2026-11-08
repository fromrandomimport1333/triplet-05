# Branches

State as inspected on 20 Oct 2026 (remote `origin`, after `git fetch --all`); `decision-math-library` was merged into `main` later that day (PR #3, `aee9fdc`). Work is identified
by branch, not by person.

```
* 3654151 (decision-math-library) merge main into decision-math-library
|\
* | 5c496fb README tweaks, usage guide
* | 9587a3d remove a python script
* | f515eda math library in brain/src (C core, Python bindings, MCP server), brain/tests
| | * 3bf9bf9 (mock/twin) twin mock
| |/
| * e572a9b (main) Merge pull request #1 from input/front-door
|/|
| * 342b7bb (input/front-door) ignore .idea
| * 7015d87 (input/front-door) input module
|/
* 054d666 project structure
* 7da8dfa init
```

| Branch | Base | Adds (vs `main`) | Tests (run on Windows, Python 3.14) | State |
|---|---|---|---|---|
| `main` | — | Triplet skeleton (`twin/`, `brain/`, `guardian/`, `docs/`, CODEOWNERS), Brain shared code `brain/src/common/`, **input module** `brain/src/input/`, configs, 19 JSON Schemas, Interface Card | 74 passed | Default branch; input module complete per `team-plan/01` |
| `input/front-door` | `054d666` | The input module | (same code as `main`) | **Merged** into `main` (PR #1); nothing left outside `main` |
| `mock/twin` (PR #2, open) | `main` | Mock Twin: `brain/mocks/twin/` (simulator, messages, MQTT service), `config/twin-mock.yaml`, 51 tests | 125 passed (74 + 51) | Not merged. Partial coverage of `team-plan/06` (no Guardian mock, no scenario runner) |
| `decision-math-library` | `main` (merged back on 20 Oct) | C99 library `citybrain` (fusion, detector, graph, decision, assignment, cache, calibration, MDP, physics) with CMake build, cffi Python bindings, a FastMCP server with 13 read-only tools, docs, C and Python tests | 169 Python passed; 12/12 C suites passed **after build workarounds** on MSVC | **Merged** into `main` (PR #3). Does not implement the planned scoring service / rule fallback. Since the merge, `python -m pytest` in `brain/` stops at collection unless the C library is built and installed (`ModuleNotFoundError: citybrain` in `tests/python/`) |
| `llm/local-service` | `main` | LLM module `brain/src/llm/`, mock LLM `brain/mocks/mock_llm/`, `config/llm.yaml`, proposal + llm-status models and schemas, 70 tests, benchmark (5 + 62 cases), LLM docs | 144 passed + live model test | In review (PR) |
| `docs/project-overview` | `main` | This `docs/` organisation | — | In review (PR) |

**Not started on any branch** (per `team-plan/00`): detection and graph DB (`src/detection`,
`src/graph`), scoring and rule fallback (`src/scoring`, `src/rules`), output and timeline
(`src/output`, `src/timeline`), Guardian mock, scenario runner and scenarios B1–B12,
`brain/docker-compose.yml`.

## Overlaps to know before merging

- `mock/twin` and `llm/local-service` both add an empty `brain/mocks/__init__.py` (identical, no
  conflict) and both edit `brain/mocks/README.md` (small conflict, keep both sections).
- `decision-math-library` puts a CMake project directly in `brain/src/` (next to the Python
  package): `brain/src/src/*.c`, `brain/src/include/`, `brain/src/.gitignore` (ignores `build/`,
  `dist/`, `*.so` for all of `src`), `brain/src/LICENSE`, `brain/src/README.md`.
- `llm/local-service` adds models to `brain/src/common/models.py` and a topic to
  `config/topics.yaml`; other branches that add models will need a schema regeneration
  (`python -m src.common.schemas`) after merging.
