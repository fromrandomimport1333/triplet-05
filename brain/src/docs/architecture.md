# Architecture

## Layering

```
   C core (src/*.c, include/citybrain/*.h)
        |
        | one flat extern "C" ABI, no allocation, no I/O, no hidden state
        v
   cffi binding (bindings/python/build_citybrain.py)
        |
        | out-of-line API mode: compiles against the real headers and the
        | built static library; produces one opaque extension module
        v
   citybrain Python package (bindings/python/citybrain/*.py)
        |
        | one wrapper module per C module; converts cffi pointers <-> plain
        | Python types; converts cb_status_t <-> CityBrainError subclasses
        v
   +---------------------------+     +---------------------------------+
   | pipeline code             |     | MCP server (mcp_server/server.py) |
   | (owned by another layer,  |     | read-only tools, JSON in/out      |
   |  not part of this repo)   |     |                                    |
   +---------------------------+     +---------------------------------+
```

Both the pipeline and the MCP server call the **same** Python package, which
calls the **same** compiled C functions. Nothing downstream of the C core
reimplements any piece of the math; every layer below it is a progressively
thinner conversion of types and a progressively more restricted surface
(fewer functions reachable, more validation on the way in), never a parallel
implementation.

## Why each boundary exists

### Why C, not C++, at the core

The public ABI needs to be callable, unmodified, from at least three places:
a cffi binding, a Z3/formal-verification harness another team member writes,
and (per the design brief) possibly other languages once this repo is pulled
into the main team repo. C has a stable, name-mangling-free, exception-free
calling convention that every one of those consumers can bind to without
reproducing C++'s ABI rules (which are not even stable across compiler
versions for the same platform). Nothing here needs classes, templates, or
exceptions — pure functions over caller-owned structs is a complete and
sufficient model for this problem, so C99 costs nothing in expressiveness
and buys unconditional callability.

### Why pure functions and caller-owned state

Three independent reasons converge on the same design:

1. **Testability.** A pure function's output is a function of its visible
   inputs; the golden-vector tests and property tests both depend on this —
   there is no hidden state to reset between cases, no ordering dependency
   between tests, and no possibility that running the test suite twice gives
   different results.
2. **Formal verification.** Another team member checks the fault-tolerance
   inequality with Z3. Z3 reasons about pure mathematical functions; a
   function with hidden mutable state is not something Z3 can check against
   a derivation at all.
3. **Threat model.** This code processes data a partner Twin may have
   falsified. A function that cannot be influenced by anything except its
   arguments is a function that cannot be left in a bad state by one
   malicious call and then behave unexpectedly on the next, honest one.

### Why the MCP layer is intentionally "dumb"

`mcp_server/server.py` contains no probability math, no clamping logic, no
derivation of any kind — every tool function's body is: validate shape,
build the citybrain Python dataclasses, call one citybrain function, shape
the result into JSON. This is deliberate and load-bearing, not merely tidy:
**the safety property "an LLM cannot dispatch anything" is a structural fact
about which functions this file defines, not an instruction any model is
asked to follow.** If this file is read end to end, there is no code path
from any tool to any actuator — not because a prompt says not to call one,
but because no such function is imported, wrapped, or reachable.

## No algorithm implemented more than once — verified

This is checked as an explicit fact, not assumed:

- `src/*.c` contains the only implementation of every algorithm (fusion,
  CUSUM/EWMA, the graph composition/clip, Hungarian assignment, the
  expected-cost rule, the cache-key hash, the calibration clip, value
  iteration, and the physics module's Gaussian LRT and physical models).
- `bindings/python/citybrain/*.py` contains **no** arithmetic on
  probabilities, log-odds, costs, or graph multipliers anywhere — every
  wrapper function's body is argument marshalling (building a `cffi`
  struct, calling exactly one `lib.cb_*` function, reading the result back
  into a dataclass) plus `errors.check(status)`. A grep for arithmetic
  operators does turn up a few hits in `mdp.py` and `assignment.py`
  (`s * n_actions + a`, `i * n_cols + j`): this is **flat-buffer index
  arithmetic**, computing where a value goes in the row-major C array the
  ABI expects when flattening a nested Python list — the same index
  formula the C header documents for that struct's layout, not a
  computation of any part of the mathematical *result*. No wrapper module
  computes a probability, a log-odds value, a cost, or a multiplier itself;
  every such number comes back from exactly one `lib.cb_*` call.
- `mcp_server/server.py` contains **no** arithmetic either — every tool body
  builds citybrain dataclasses from its JSON arguments, calls exactly one
  `citybrain.<module>.<function>()`, and returns a dict built from the
  result's fields.
- `bindings/python/citybrain/physics.py` follows the same rule as every
  other wrapper module: no probability, rate, concentration, or speed is
  computed in Python -- each function builds a `cffi` call, calls exactly
  one `lib.cb_physics_*` function, and returns the result. The MCP
  `physics_*` tools in turn call exactly one `citybrain.physics.*` function
  each, with no composition or arithmetic of their own.
- `tests/vectors/gen_vectors.py` is the **one** intentional exception: it is
  a second, independent implementation of the math, in Python with
  arbitrary-precision `Decimal` arithmetic, and it exists *specifically* so
  that the C tests and the Python tests are checked against a third,
  independently-derived source of truth rather than against the C library's
  own output. This is not a violation of the "implemented once" rule — it is
  the thing that makes the golden-vector tests meaningful rather than
  circular. See `docs/testing.md`.

## Call graph: who calls what

```
cb_fusion_posterior()              <-- src/fusion.c (THE implementation)
  ^                 ^
  | cffi call       | cffi call
  |                 |
citybrain.fusion.fuse()   (bindings/python/citybrain/fusion.py)
  ^                           ^
  | import + call             | import + call
  |                           |
(pipeline, outside this repo)  mcp_server.server.fuse_evidence()  (tool)
```

The same shape repeats for every other function: one `cb_*` C symbol, one
Python wrapper function that calls it, and zero to many callers of that
wrapper (the MCP server being one specific caller among possibly several).

## What this component is NOT responsible for

Stated here plainly so the boundary is not accidentally crossed later:

- **Reading validation / signature, nonce, timestamp checks.** A different
  layer's job entirely; this library never touches raw partner bytes.
- **Trust/verdict interpretation.** This library accepts a plain numeric
  weight in `[0, 1]` per evidence item and is agnostic to how it was
  produced. It has no function, type, or constant that knows about
  `trusted`/`suspect`/`rejected` or any other verdict vocabulary. Mapping a
  categorical verdict (or any other external signal) onto that `[0, 1]`
  weight is the integrating application's responsibility.
- **Detecting a compromised, frozen, or faulty sensor/channel.** The
  change-point detector in `citybrain/detector.h` answers "has this
  already-trusted measurement stream shown a sustained shift," never "can
  this stream be believed." It does not attempt, approximate, or duplicate
  whatever a trust/security watchdog layer does; it has no concept of sensor
  identity, authenticity, or compromise at all.
- **The incident lifecycle state machine, the safety verifier's
  whitelist/idempotency checks, actuator dispatch, or the LLM advisor's
  prompting.** None of these are implemented, stubbed with real logic, or
  assumed by this repo. Where a test needs a stand-in for one of these (for
  example, a minimal fixture that plays the role of "the pipeline" in an
  integration-style test), it is marked in-code as `# stub -- real
  implementation owned by another layer` and contains no logic beyond what
  the test needs to drive the citybrain call.

## Optional module: incident physics

`citybrain/physics.h` (`src/physics.c`, `citybrain.physics` in the Python
package, and seven read-only MCP tools prefixed `physics_`) adds optional,
pluggable likelihood-ratio and expected-value functions grounded in cited
physical models (shallow-reservoir mass balance for flood, t-squared growth
plus Gaussian plume dispersion for fire, the Greenshields speed-density
relation for traffic), so that `cb_fusion_posterior`'s evidence can be
grounded in domain physics instead of an arbitrarily configured number when
a caller wants that. It is purely additive: nothing in `fusion.h`,
`detector.h`, `graph.h`, `decision.h`, or any other module was changed to
add it, no existing function's signature, behaviour, or safety guarantee is
altered, and a likelihood ratio this module computes is ordinary input to
`cb_evidence_t` with no special treatment or exemption from the
fault-tolerance cap (`docs/math_reasoning.md` §6.6 demonstrates this in
code, not just in prose). Like the change-point detector, it only ever
answers "how consistent is this reading with a real incident," never
whether the reading should be trusted — see `docs/math_reasoning.md` §6.1
for the restated scope boundary.

## Diagram: adversarial data flow through the layers

```
          Twin (partner, untrusted)
                   |
   [reading gate, Guardian verdict, lifecycle -- NOT this repo]
                   |
                   v
       caller assigns: likelihood_ratio, weight in [0,1], group_id
                   |
                   v
        citybrain.fusion.fuse()  -- THIS REPO, pure function
                   |
          posterior probability (bounded by the fault-tolerance
          inequality: see docs/math_reasoning.md)
                   |
                   v
   [safety verifier, actuator dispatch -- NOT this repo]
```

Everything above and below the single boxed step is explicitly out of scope;
this repo's entire contribution to the request/response path is that one
pure, provably-bounded computation (plus its sibling computations: change
detection, graph adjustment, decision, assignment).
