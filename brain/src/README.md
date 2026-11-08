# citybrain

A generic, portable **deterministic decision library**: bounded-influence
evidence fusion, change-point detection, a bounded multiplier graph,
expected-cost decisions, optimal assignment, cache-key algebra, bounded
calibration updates, a tabular MDP solver, and an optional, pluggable
incident-physics module (flood, fire, traffic likelihood models grounded
in cited published physics).

Built as the math/deterministic decision layer of a disaster/incident
response system (TSYP14, City Brain Convergence) — but the library itself
has no knowledge of that system. Every public function takes probabilities,
weights, likelihood ratios, costs, and indices; nothing in the public
interface refers to sensors, incident types, or any particular verdict
vocabulary. See `docs/architecture.md` for the full boundary statement.

## The central guarantee

**A single falsified evidence source, bounded by a configurable
likelihood-ratio cap, cannot by itself cross an action threshold —
reaching the threshold requires a provable minimum number of independent
corroborating sources.** This is derived, implemented, cross-checked
against its own compiled code, and tested against tens of thousands of
randomized configurations and a set of named adversarial scenarios. See
`docs/math_reasoning.md` for the derivation and `docs/testing.md` for the
evidence.

```python
import citybrain

cfg = citybrain.fusion.FusionConfig(lr_cap=8.0, prior=0.02)
result = citybrain.fusion.fuse_independent(
    cfg, [citybrain.fusion.Evidence(likelihood_ratio=1e300, weight=1.0, group_id=0)]
)
result.posterior  # 0.1403508771929825 -- well under a 0.20 action threshold,
                   # however extreme the single fabricated reading claims to be
```

## Quickstart

Source lives in `brain/src`, tests in `brain/tests`. Run these from
`brain/` (one level up from this file):

```bash
# C core
cmake -S src -B src/build -DCMAKE_BUILD_TYPE=Release
cmake --build src/build
ctest --test-dir src/build --output-on-failure

# Python bindings -- one command, builds the C core automatically
pip install -e "src/bindings/python[test]"
pytest tests/python/

# MCP server (read-only tools for an LLM advisor)
pip install -e src/mcp_server
python src/mcp_server/demo.py
```

Full details: `docs/build.md`.

## Repository layout

```
brain/
  src/
    include/citybrain/       public C header(s) -- the one source of truth
                              (includes physics.h, the optional physics module)
    src/                      C implementation (includes physics.c)
    bindings/python/
      citybrain/              pythonic wrapper package (cffi, out-of-line API mode)
    mcp_server/               read-only MCP tool server
    docs/
      architecture.md         layering, why each boundary exists, call graph
      build.md                exact build/install/run steps
      api_reference.md        one entry per public function
      math_reasoning.md       the fault-tolerance derivation and proof-vs-code cross-check
      testing.md               what's tested, actual results, known limitations
      usage.md                 how to call this from elsewhere in brain/
      City_Brain_Math_Core_Reasoning.pdf   standalone reasoning document (+ .md source)
    CMakeLists.txt
  tests/
    *.c                       C unit, adversarial, property-based tests
    bench/                    benchmarking
    vectors/                  golden vectors, shared by the C and Python suites
    python/                   Python test suite, same golden vectors as the C tests
```

## What this is responsible for, and what it is not

This library implements the math/deterministic decision layer only. It does
**not** implement, and must not be mistaken for, the reading validation
gate, the Guardian verdict interpretation, the incident lifecycle state
machine, the safety verifier, the actuator dispatch path, or the LLM
advisor's prompting — those belong to other layers, in other parts of the
system this library was built for, and are not part of this repository.
Full statement: `docs/architecture.md`, "What this component is NOT
responsible for."

## Documentation map

| document | read this for |
|---|---|
| `docs/architecture.md` | the four-layer stack, why each boundary exists, what this is not responsible for |
| `docs/build.md` | exact build/install/run commands and toolchain versions |
| `docs/api_reference.md` | every public function: C signature, Python signature, errors, a worked example |
| `docs/math_reasoning.md` | the fault-tolerance inequality, its proof, its cross-check against the compiled code, algorithm-choice reasoning, related work |
| `docs/testing.md` | what's tested, how to run it, actual results, honest known limitations |
| `docs/usage.md` | how to call this from elsewhere in `brain/` |
| `docs/City_Brain_Math_Core_Reasoning.pdf` | standalone reasoning document for a non-coder reader (judge, teammate deciding whether to trust this component) |

## Versioning

This component follows semantic versioning starting at `v1.0.0`. Breaking
changes to any public C function's signature or documented behavior bump
the major version; new functionality that doesn't break existing callers
bumps the minor version; bug fixes bump the patch version. The CMake
package-config file installs a version file so `find_package(citybrain)`
can request a compatible range.

## License

MIT (see the Python package metadata in `bindings/python/pyproject.toml`;
the C core and all other components share the same license).
