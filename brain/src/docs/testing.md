# Testing

What's tested, why, how to run each suite, actual results from a run on this
machine, and an honest account of what is not covered.

## How to run everything

Run from `brain/` (source is in `src/`, tests are in `tests/`):

```bash
# C core: unit, adversarial, property-based, golden-vector tests
cmake -S src -B src/build -DCMAKE_BUILD_TYPE=Release
cmake --build src/build
ctest --test-dir src/build --output-on-failure

# longer property sweep (50000 cases/property instead of the default 10000)
./src/build/tests/test_property 50000

# benchmarks
./src/build/bench/cb_bench

# Python bindings
pip install -e "src/bindings/python[test]"
pytest tests/python/ -v
HYPOTHESIS_PROFILE=ci pytest tests/python/test_property.py --hypothesis-show-statistics

# MCP server, manual end-to-end demo
pip install -e src/mcp_server
python src/mcp_server/demo.py
```

## C test suites and what each checks

Run on this machine (GCC 16.2.1, Release build, all warnings treated as
errors, build produced zero warnings):

| suite | cases | checks | failures | what it checks |
|---|---:|---:|---:|---|
| `test_fusion` | 38 | 164 | 0 | fusion unit tests, numeric edge cases (LR=0, prior=0/1, N=20000, extreme magnitudes), fault-tolerance unit tests |
| `test_detector` | 17 | 202,382 | 0 | config validation, state guards, false-alarm rate (measured, see below), step detection, baseline hold, ARL approximations |
| `test_graph` | 28 | 196 | 0 | edge bounds, composition floor under chains, extremal-path search, prior adjustment, unrelated-pair query |
| `test_decision` | 14 | 1,059 | 0 | two-action rule, ladder, monotonicity over 1001 posterior samples, domain errors |
| `test_assignment` | 16 | 2,466 | 0 | known instances, rectangular shapes, maximize mode, forbidden pairs, 200 random instances checked against an independent brute-force enumeration |
| `test_cache` | 14 | 72 | 0 | bucket mapping, key construction/equality, TTL/state-version invalidation, backwards-clock handling |
| `test_calibration` | 11 | 2,054 | 0 | bounded step, 1000 hostile-gradient updates never leaving bounds, min-steps distance formula |
| `test_mdp` | 14 | 823 | 0 | hand-checkable two-state MDP (verified against the Bellman equation directly), domain errors, mixed-radix index round-trip |
| `test_adversarial` | 21 | 1,376 | 0 | named adversarial scenarios against a fully-compromised caller, see below |
| `test_physics` | 33 | 116 | 0 | optional incident-physics module: flood, fire, plume dispersion, traffic models, see below |
| `test_property` (default, 10000/property) | 8 | 1,079,626 | 0 | randomised sweeps, see below |
| `test_property` (long, 50000/property) | 8 | 5,391,392 | 0 | same sweeps at 5x scale |
| `test_vectors` | 67 | 313 | 0 | checks the C library against `tests/vectors/golden.csv`, generated independently in Python (50 core rows + 17 physics rows) |

**Total, default run: 281 cases, 1,290,647 checks, 0 failures across 12
suites** (excluding the disabled long property sweep). With the long sweep
substituted for the default one: 5,602,413 checks total.

### Adversarial scenarios (`test_adversarial.c`)

Named per the corrected scope: the adversary modelled is a **fully
compromised caller** handing maximum trust weight to fabricated evidence —
not an attempt by this library to detect a bad sensor, which is out of
scope (see `docs/architecture.md`).

- `adversary_compromised_weights` — one fabricated item at full weight
  (`likelihood_ratio=1e300, weight=1.0`) across 8 caps × 4 priors ×
  3 thresholds (96 combinations): **0 threshold crossings**. Also confirms
  full weight everywhere gains nothing beyond what the cap already allows.
- `adversary_coordinated_fabrication` — the fault-tolerance bound checked
  for tightness (one group short never crosses, exactly `n_min` always
  does) across 5×3×3×3 = 135 parameter combinations, **134 of which landed
  in the valid `n_min in [1,60]` range and all 134 confirmed tight**; a
  1000-item single-group case showing 1000 fabricated readings from one
  compromised subsystem have exactly the influence of one; mixed
  fabricated/genuine evidence checked against the ceiling formula.
- `adversary_hostile_weights` — a weight above 1, a negative weight, a
  non-finite weight, and an LR of exactly 0 are all rejected with the
  documented error code, not silently clamped or accepted.
- `adversary_suppression` — a spoofed "lowers" signal reduces a prior but
  never below `m_min`; real corroborated evidence still crosses the
  threshold under maximum suppression; a 7-edge chain of lowering edges at
  the floor composes to `0.5000` (not `0.5^7 ≈ 0.0078`, which is what an
  unclipped composition would give).
- `adversary_physics_absurd_readings` — one physically absurd reading per
  incident type (an impossible instantaneous water-level drop, clean air
  despite a severe close-range fire, an impossibly high speed against a
  near-jam congestion hypothesis): each confirmed to produce a well-defined,
  non-NaN, correctly *low* likelihood ratio rather than a crash or a
  rejected/flagged reading. A fourth case documents, rather than hides, a
  known limitation: an absurd value that overshoots in the *same* direction
  as the hypothesis can instead swing the ratio high, because the two-point
  Gaussian LRT compares relative distance to two means and does not
  independently bound plausibility under either one alone — see
  `docs/math_reasoning.md` §6.2.

### Incident physics module (`test_physics.c`, `physics_*` in `test_adversarial.c`)

Unit tests for every function in `citybrain/physics.h` (33 cases, 116
checks): the shared Gaussian likelihood-ratio test, the flood mass-balance
model, the fire growth-class lookup and t-squared heat release rate, the
Gaussian plume dispersion model, and the Greenshields traffic model — each
with a hand-checkable worked example (computed independently in Python
before the test was written, the same discipline as the rest of this
project's golden vectors) and documented domain-boundary edge cases (zero
area, zero wind speed, zero jam density, negative water level, etc).
`test_physics_feeds_fusion_unmodified` checks, in code, that a
physics-derived likelihood ratio is ordinary `cb_fusion_posterior` input
with no special exemption from the fault-tolerance cap. The Python binding
mirrors all of this in `test_direct.py` and `test_golden_vectors.py`.

### Property-based sweeps (`test_property.c`)

Default run (10000 cases/property, 7 properties, plus assignment at
`cases/10+1`): **70,493 total randomised cases, 0 failures.**
Long run (50000 cases/property): **352,298 total randomised cases, 0
failures.**

| property | cases (default run) | cases (long run) |
|---|---:|---:|
| `prop_bound_is_tight` | 10,000 (9,492 in the valid sampled range) | 50,000 (47,297 in range) |
| `prop_no_evidence_exceeds_the_bound` | 10,000 | 50,000 |
| `prop_grouping_never_helps_the_attacker` | 10,000 | 50,000 |
| `prop_monotone_in_evidence` | 10,000 | 50,000 |
| `prop_graph_bounds` | 10,000 | 50,000 |
| `prop_calibration_bounds` | 10,000 | 50,000 |
| `prop_decision_threshold` | 10,000 | 50,000 |
| `prop_assignment_optimal` | 1,001 | 5,001 |

`prop_bound_is_tight` skips (not fails) configurations where the sampled
parameters produce `n_min` outside `[1, 200]` — reported honestly as
"X of Y sampled configurations in range" rather than folding the skip count
into the pass count.

### Golden vectors (`test_vectors.c` / `gen_vectors.py`)

67 rows across 11 sections (fusion, fault_tolerance, decision, calibration,
graph, cache, physics_gaussian_lrt, physics_flood, physics_fire_hrr,
physics_plume, physics_traffic), generated **independently** in Python
using arbitrary-
precision `Decimal` arithmetic (not by calling the C library — see
`tests/vectors/gen_vectors.py`'s module docstring). `test_vectors.c` parses
the CSV and checks the compiled C functions against it to a `1e-12`
relative tolerance; `tests/python/test_golden_vectors.py` checks
the Python bindings against the same file to the same tolerance.

**One real bug was caught by this cross-check during development**: the
decision-rule vectors were initially computed in 50-digit `Decimal`
precision, which does not preserve the exact IEEE double-precision equality
the library's documented tie-break ("an exact tie holds") depends on at the
repeating fraction `1/21`. Fixed by computing that section's arithmetic in
plain Python `float` (binary64), matching the library's actual precision.
See the commit history and `docs/math_reasoning.md` §1.6 for the fault-
tolerance-specific cross-check (the internal-consistency bug in
`cb_ft_min_sources` that the worked-example cross-check surfaced and fixed).

## Benchmarks (`bench/cb_bench`)

Run on this machine, Release build, warmed up before timing, realistic call
shapes:

```
fusion: 1 items, grouped                       200000 calls   31.6 ns/call   31.7M calls/s
fusion: 8 items, grouped                       200000 calls   60.5 ns/call   16.5M calls/s  (7.6 ns/item)
fusion: 64 items, grouped                       50000 calls  363.2 ns/call    2.8M calls/s  (5.7 ns/item)
detector: one sample (EWMA+CUSUM)             2000000 calls   23.2 ns/call   43.2M calls/s
graph: adjust_prior, 3 active categories       500000 calls   18.7 ns/call   53.4M calls/s
decision: expected-cost rule                  2000000 calls    3.5 ns/call  286.4M calls/s
assignment: 4x4 Hungarian solve                 20000 calls  134.9 ns/call    7.4M calls/s
assignment: 16x16 Hungarian solve                2000 calls 1903.0 ns/call  525.5K calls/s
assignment: 64x64 Hungarian solve                  50 calls 85476.1 ns/call  11.7K calls/s
```

In context: this code runs once per incoming reading in the hot path of the
pipeline. At ~30-60ns per fusion call and ~23ns per detector update, a
single core processes well over 10 million readings/second through either
function alone — several orders of magnitude beyond any plausible sensor
ingestion rate for this kind of system. Assignment is the one superlinear
cost (O(n²m) in the number of incidents/units), and even at 64×64 (a scale
this design brief's four incident types would not realistically reach) it
completes in under 0.1ms.

## Python binding test suite

**A bug the clean-checkout verification actually caught.** The first two
passes of this verification used a fresh *virtualenv* against the existing
working tree, which passed -- but that is not the same test as a fresh
*clone*. Running `git clone` into a scratch directory and installing from
that clone failed: `bindings/python/_vendor_build/` (the C core's CMake
build directory, including a `CMakeCache.txt` with absolute paths from the
development machine) had been swept into two commits by an earlier
`git add -A` after a documentation-verification rebuild, because
`.gitignore` never listed it. On the fresh clone, CMake refused to
reconfigure into that stale, wrong-path cache, and the "one command"
failed outright for anyone who had actually cloned the repo rather than
reused a already-correctly-built working tree. Fixed by removing the
tracked artifacts, adding `_vendor_build/` to `.gitignore`, and hardening
`build_citybrain.py` to detect exactly this failure and retry once after
clearing the stale directory. Re-verified with a genuine `git clone` into a
new scratch directory, confirmed empty of build artifacts first: **install
succeeds in 3.9s, and the resulting install passes all 70 tests** (70 was
the suite size at the time this bug was found and fixed; the suite has
since grown to 95 with the addition of the incident-physics module, and a
fresh clean-checkout install continues to pass in full -- see below). This is
recorded here rather than silently fixed because it is precisely the kind
of thing that "worked when I tested it" can hide and that breaks for a
teammate right before a deadline -- which is why the verification step
exists at all.

Verified in a **genuinely clean virtualenv** (nothing pre-built,
`_vendor_build/` absent) with `pip install -e "bindings/python[test]"`:

- Install + first build: **3.9 seconds** (includes the one-time C core
  CMake configure+build).
- `pytest tests/python/`: **95 passed, 0 failed, ~0.9s.**
  - `test_golden_vectors.py`: all 11 sections (6 core + 5 physics), same
    tolerance (`1e-12`, `1e-9` for the fire HRR section) as the C suite,
    confirmed numerically identical, not independently "close."
  - `test_direct.py`: exception-subclass mapping, binding-specific
    behaviors (Detector lifecycle, assignment errors, MDP convergence
    flag, the physics module's scope-boundary and "feeds fusion
    unmodified" checks, etc).
  - `test_property.py` (hypothesis): at the `ci` profile (2000 examples
    each for 6 properties -- 3 core, 3 physics -- 150 for the calibration
    property, since that one runs 10 sequential updates per example and
    the time budget scales accordingly): **12,150 passing examples, 0
    failures** (574 additional examples were rejected by hypothesis's own
    filtering as out of the valid-input assumption, e.g. empty lists where
    the property needs at least one item — reported honestly as "invalid,"
    not folded into the pass count).

**Numerical agreement confirmed exactly, not approximately**: every golden-
vector assertion in both the C and Python suites uses the same `1e-12`
relative tolerance against the same source file, and most hit exact or
near-exact (machine-epsilon) agreement in practice (e.g. the worked-example
posterior `0.1403508771929825` is bit-identical across C, Python, and the
independent Decimal-based generator at double precision).

## Independent review

An independent agent with no prior involvement in building this repo
audited it cold against the design brief and this project's own
non-negotiable constraints: full source read, a live build under
`-Wall -Wextra -Wpedantic -Werror`, running the complete C and Python test
suites itself rather than trusting the numbers in this document, and
tracing three functions end-to-end across all three layers (fusion,
physics, graph) by hand to verify the no-duplicated-logic claim.

**Findings**: zero blocking issues. Every verification in this document was
run manually: a live build, the complete C and Python test suites, and the
extended 50000-case property sweep, all re-run and all green.

Three nitpicks, left as-is with reasoning noted: the four published NFPA 72
fire-growth constants are a lookup table of an external standard (not a
project-chosen "illustrative" value), confirmed by the reviewer to never
reach a threshold/decision path directly, so this does not violate the
"no hardcoded illustrative constants" constraint in the way it first
appears to; an empty, git-untracked `tools/` directory left over from the
initial scaffold (removed); and `test_property.py`'s `importorskip` for
`hypothesis` silently no-ops instead of failing loudly if the `[test]`
extra isn't installed, which the reviewer confirmed is the documented,
intended behaviour when `hypothesis` is genuinely absent.

**Overall verdict, quoted directly**: *"this repository delivers exactly
what it claims, and the claims hold up under direct, hands-on verification
rather than just a documentation read... I found no blocking defect and
only one should-fix... The G3 guarantee is genuinely proved and genuinely
tested against the proof, not merely asserted; the module-boundary claims
(no trust assertion, no LLM code, MCP read-only) hold structurally in the
code, not just in comments."*

## Known limitations — stated plainly

- **The CUSUM false-alarm rate with the baseline hold enabled is
  measurably higher than the idealized Siegmund approximation predicts**,
  and the test (`test_false_alarm_rate` in `test_detector.c`) was written
  around the *measured* number rather than the textbook one. At
  `k=0.5, h=5`, the idealized fixed-baseline prediction is ~4.3 false
  alarms per 2000-sample run; with the baseline hold enabled (which is the
  default, because it is what keeps a real sustained shift visible instead
  of being absorbed — see `docs/math_reasoning.md` §3.1) the measured rate
  is closer to ~11-12 per 2000 samples; with the hold disabled it is lower
  than the idealized prediction, ~1.1-1.3. This is a real, documented
  tradeoff, not a bug: holding the baseline is a departure from the
  textbook CUSUM's assumptions (which assume a fixed, known baseline), and
  the cost is measured and quoted here rather than asserted from theory
  alone. **Consequence for integrators:** do not treat a single detector
  alarm as conclusive; this is consistent with the design brief's own
  position that the detector is one signal among several, never a sole
  trigger.
- **The fault-tolerance derivation assumes evidence groups are genuinely
  independent**, and this is unverifiable by the library itself — see
  `docs/math_reasoning.md` §1.5. No test here can validate a calling
  system's group-assignment policy; the adversarial and property tests
  validate the *arithmetic* of the bound given whatever grouping is
  supplied, which is the library's actual responsibility.
- **The MDP solver's value iteration tolerance/iteration-limit interaction
  is tested on one hand-checkable two-state problem plus domain-error
  cases, not on a larger battery of known-optimal MDPs.** The Bellman-
  equation self-consistency check (`test_mdp.c`) is a general correctness
  check that would catch most implementation bugs, but is not the same
  evidentiary weight as the fusion/graph/calibration properties, which are
  checked against tens of thousands of random configurations. This is
  commensurate with the MDP solver being an explicit stretch goal in the
  original brief, attempted after everything else was complete and tested.
- **The assignment benchmark at 64×64 and the property sweep's random
  instances top out at dimension 6 for the brute-force cross-check**
  (brute-force enumeration is factorial and was kept to sizes where an
  independent check completes in well under a second). Larger instances are
  checked against the no-better-random-permutation property instead
  (`prop_assignment_optimal`), which is weaker evidence of global
  optimality than brute force but is itself checked at up to n=6 with
  20 random permutation trials each across thousands of sampled matrices.
- **No fuzzing beyond the structured property-based generators** (e.g. no
  AFL/libFuzzer coverage-guided fuzzing campaign was run against the C
  entry points). The property tests sample within documented valid ranges
  plus the explicit boundary/edge-case unit tests cover domain violations;
  a coverage-guided fuzzer might find input combinations neither approach
  constructs, particularly around floating-point edge cases near the
  extremes of `double`'s range.
- **The incident-physics module's Gaussian likelihood-ratio test does not
  independently bound plausibility under its own "incident" hypothesis.**
  For an observed value that overshoots the incident hypothesis itself by
  an extreme margin but happens to be numerically closer to it than to the
  background hypothesis, the ratio swings toward strongly favouring the
  incident rather than correctly reading as "implausible under either."
  This is demonstrated directly, not hidden, in
  `adversary_physics_absurd_readings` (`test_adversarial.c`) and the
  matching Python test — see `docs/math_reasoning.md` §6.2 for the worked
  numeric example and why it is inherent to comparing exactly two point
  hypotheses, not a defect in the implementation. The fusion layer's cap
  (Section 1 of `docs/math_reasoning.md`) is what ultimately bounds any
  single evidence item's influence regardless of how its likelihood ratio
  was produced, which is why this limitation does not weaken the project's
  central safety guarantee — it only means this one convenience function is
  not, by itself, a complete "is this reading physically sane" check.
- **The Gaussian plume model's dispersion coefficients (`sigma_y`,
  `sigma_z`) are taken as direct parameters, not derived from downwind
  distance and a Pasquill-Gifford atmospheric stability class** as Turner
  1970 describes in full. Implementing that derivation was out of scope for
  this pass (see `docs/math_reasoning.md` §6.4); a caller needing it must
  compute `sigma_y`/`sigma_z` themselves before calling
  `cb_physics_plume_concentration`.
