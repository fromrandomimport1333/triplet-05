# Math reasoning

This document derives the fault-tolerance inequality that `citybrain/fusion.h`
implements, cross-checks the derivation against the compiled code, explains
the calibration bound, explains why each algorithm was chosen over a simpler
alternative, and places the design relative to the published literature it
draws on.

Written for a technical reader who has not necessarily read the C source.

---

## 1. The fault-tolerance inequality

### 1.1 Setup

A binary hypothesis `H` ("the thing being monitored for is real") has prior
probability `p0`, `0 < p0 < 1`. Evidence arrives as a sequence of independent
groups `g = 1, 2, ..., n`. Each group contributes a capped log-likelihood-ratio
to the posterior log-odds. Concretely, from `include/citybrain/fusion.h`:

```
posterior log-odds   L = L0 + sum_g A_g
A_g                   = clip( sum_{i in g} w_i * clip(ln LR_i, -ln c, +ln c),
                               -ln c, +ln c )
```

where `c > 1` is the likelihood-ratio cap, `w_i in [0, 1]` is the weight
attached to evidence item `i`, and `L0 = logit(p0)`.

The quantity we want to bound is: **given a cap `c`, a prior `p0`, and an
action threshold `T`, how many independent capped groups does it take to push
the posterior probability to or past `T`?**

This is the formula another team member encodes and checks with Z3. It is
written to be unambiguous about domains and to state exactly which guarantee
it proves.

### 1.2 Variables and domains

| symbol | meaning | domain |
|---|---|---|
| `c` | likelihood-ratio cap | `c > 1`, finite |
| `p0` | prior probability of `H` | `0 < p0 < 1` |
| `T` | action threshold (probability) | `0 < T < 1` |
| `w` | maximum admitted per-item weight | `0 < w <= 1` |
| `m` | prior-odds multiplier from any upstream adjustment (e.g. a correlation graph) | `m > 0`, finite |
| `n` | number of independent capped evidence groups | `n` a non-negative integer |

A group's maximum possible contribution to the log-odds is `w * ln(c)`,
because every item in a group is weighted by at most `w` and every item's
log-likelihood-ratio is clipped to `[-ln c, +ln c]` *before* the group sum is
itself clipped to the same interval — so the group-level clip is never the
binding one when `w <= 1`; the per-item clip already enforces it.

### 1.3 Derivation

Define the adjusted prior log-odds:

```
L0' = logit(p0) + ln(m)
```

With `n` independent groups, each contributing its maximum possible amount
`w * ln(c)` in the direction of `H`, the largest reachable posterior log-odds
is

```
L_max(n) = L0' + n * w * ln(c)
```

and the largest reachable posterior probability is

```
p_max(n) = logistic(L_max(n)) = 1 / (1 + exp(-L_max(n)))
```

This is monotone increasing in `n` because `w * ln(c) > 0` (since `w > 0` and
`c > 1`) and `logistic` is strictly increasing. So there is a well-defined
smallest `n` at which `p_max(n) >= T`:

```
n_min = min { n in Z_{>=0} : L0' + n * w * ln(c) >= logit(T) }
```

Solving the inner inequality for `n`:

```
n >= (logit(T) - L0') / (w * ln(c))
```

and since `n` must be a non-negative integer,

```
n_min = max( 0, ceil( (logit(T) - L0') / (w * ln(c)) ) )
```

**This is the formula.** `logit` and `logistic` are the standard
```
logit(p)     = ln(p / (1 - p))
logistic(L)  = 1 / (1 + exp(-L))
```

### 1.4 What is guaranteed (G3)

> **G3** (design brief, Guarantees list): *one falsified evidence group cannot
> trigger an irreversible action.*

Restated precisely: **for any configuration with `c`, `p0`, `T`, `w`, `m`
inside the domains above, if the number of groups a single fault or a single
adversary can control is strictly less than `n_min`, then no assignment of
likelihood ratios to those groups can make the fused posterior reach `T`.**

This follows directly from the derivation: `p_max(n)` is the *supremum* over
all possible evidence in `n` groups (every item in every group pushed to the
cap, in the direction of `H`, at maximum weight). If `n < n_min`, then by
definition of `n_min` as the smallest `n` with `p_max(n) >= T`, we have
`p_max(n) < T`, and since the actual posterior for any real evidence in those
`n` groups is at most `p_max(n)` (the clip is an upper bound on every group's
contribction), the actual posterior is also `< T`.

The guarantee is unconditional on the *content* of the falsified evidence —
arbitrarily large or small raw likelihood ratios, arbitrarily chosen
weights up to `w` — because the cap already bounds the worst case before any
of those choices are made. It is **not** unconditional on the number of
groups the adversary controls, which is exactly the point: the inequality
makes explicit how many independent groups are needed, which is a number the
pipeline's group-assignment policy is responsible for keeping larger than any
single fault or adversary can reach (see Section 1.6).

### 1.5 Operational meaning of "independent" (load-bearing assumption)

The library does not and cannot verify that two evidence groups are actually
independent — it trusts the caller's `group_id` assignment completely. This
is the single assumption the entire guarantee rests on, so it must be stated
operationally, not left as a mathematical abstraction.

**Working definition used in this design:** two evidence items belong to
different groups only if a single fault or a single adversary could not
plausibly cause both items to carry fabricated or manipulated values at once.
For the City Brain pipeline specifically (an example application of this
library, not a requirement of it), the brief's own section 10 implies the
operational split is along three axes simultaneously:

- **sensor modality** (a water-level sensor failing does not make a traffic
  camera lie),
- **physical zone** (a compromised device in one zone does not reach a sensor
  in another), and
- **originating subsystem/partner channel** (one compromised upstream
  component is one group, however many readings it emits).

Two items are placed in different groups only when they differ on **all
three** axes. Splitting on fewer axes (e.g. treating every individual sensor
ID as its own group even when ten sensor IDs sit behind one compromised relay)
silently weakens G3 by exactly the number of groups the attacker gets for
free — the formula still holds, but `n_min` no longer means what the
guarantee needs it to mean, because the real number of independent sources
an adversary can control is smaller than `n_min` claims is required. **This
is a pipeline-layer responsibility; this library enforces the arithmetic of
the bound, not the correctness of the grouping that the bound is evaluated
against.**

### 1.6 Cross-check against the design brief's worked example

The brief states (section 6): *"with a cap of 8x and a typical threshold, a
single falsified source raises the chance of 'fire' to about 14%, still well
under the ~20% needed to act; two independent sources are needed to cross
it."*

Plugging `c = 8`, `p0 = 0.02`, `T = 0.20`, `w = 1`, `m = 1` into the formula:

```
L0'          = logit(0.02)              = ln(0.02/0.98) = -3.8918...
ln(c)        = ln(8)                    =  2.0794...
logit(T)     = logit(0.20)              = ln(0.2/0.8)   = -1.3863...
n_min        = ceil( (logit(0.2) - logit(0.02)) / ln(8) )
             = ceil( (-1.3863 - (-3.8918)) / 2.0794 )
             = ceil( 2.5055 / 2.0794 )
             = ceil( 1.2049 )
             = 2

p_max(1) = logistic( logit(0.02) + 1 * ln(8) ) = logistic(-1.7918) = 0.1403508771929825...
p_max(2) = logistic( logit(0.02) + 2 * ln(8) ) = logistic( 0.2876) = 0.5663716814159292...
```

**Both derivation-by-hand and compiled library agree:**

| quantity | hand derivation | `cb_ft_*` compiled output | match |
|---|---|---|---|
| `n_min` | 2 | `cb_ft_min_sources(c=8, p0=0.02, T=0.20) = 2` | yes |
| `p_max(1)` | 0.1403508771929825 | `cb_ft_max_posterior(..., 1) = 0.1403508771929825` | yes, bit-identical |
| `p_max(2)` | 0.5663716814159292 | `cb_ft_max_posterior(..., 2) = 0.5663716814159292` | yes, bit-identical |

Both agree with the brief's own illustrative figures: one source reaches
"about 14%", below the "~20%" threshold; two sources cross it ("0.5664 > 0.20"
— well past, since the brief's "two sources" figure is illustrative and not
itself a precise target).

**This cross-check surfaced one real bug during development**, which is
recorded here rather than silently fixed, per the project's reporting
standard: `cb_ft_min_sources` originally settled an *exact* boundary case
(where `n_min` lands precisely on an integer) by comparing in log-odds space,
while `cb_ft_max_posterior` and `cb_ft_check_invariant` compare in probability
space after converting through `logistic()`. At an exact boundary the two
comparisons could disagree by one unit in the last place of a `double`,
because `logistic()` is not an exact bijection in floating point — meaning
`cb_ft_min_sources` could return an `n` that `cb_ft_check_invariant` then
reported as unsafe for that same `n`. **The derivation was correct; the
*implementation's internal consistency* was the bug.** The fix (see
`src/fusion.c`, function `ft_reaches`) makes every fault-tolerance function
share one predicate — convert to probability, then compare against `T` — so
`cb_ft_min_sources`, `cb_ft_max_posterior`, and `cb_ft_check_invariant` can
never disagree with each other by construction. A property test
(`prop_bound_is_tight` in `tests/test_property.c`) checks this holds across
tens of thousands of randomly sampled configurations, not just the one
worked example.

### 1.7 The adjusted-prior multiplier `m`

`m` is not invented for this derivation; it is exactly the composed
multiplier the correlation graph (`citybrain/graph.h`) can apply to a prior.
Section 4 below explains why that multiplier is itself bounded to
`[m_min, m_max]` under composition. Feeding the worst case `m = m_min` into
`cb_ft_min_sources` answers: *"if an adversary achieves the maximum possible
suppression the graph permits, how many independent evidence groups are still
needed?"* — this is exactly the adversarial scenario
`adversary_suppression` in `tests/test_adversarial.c` exercises, and the
derivation above shows the answer is always a finite, boundable number (never
"suppression makes the required evidence unbounded"), because `m_min > 0` by
construction of the graph's floor.

---

## 2. Calibration bound reasoning

`citybrain/calibration.h` implements:

```
theta_next = clip( theta + clip(rate * gradient, -max_step, +max_step), lo, hi )
```

**Claim:** for any sequence of gradients (including an adversarially chosen
one, since this library takes the gradient as given and does not evaluate
where it came from), `theta` never leaves `[lo, hi]`, and moving `theta` by a
distance `d` costs at least `ceil(d / max_step)` accepted updates.

**Proof sketch:** the outer `clip(..., lo, hi)` is applied unconditionally to
every update's result, so `theta_next in [lo, hi]` by construction regardless
of what `rate * gradient` evaluates to — this holds even if `rate * gradient`
is `+inf`, since the inner clip to `[-max_step, max_step]` runs first and
produces a finite value before the addition. Each accepted update moves
`theta` by at most `max_step` in absolute value (the inner clip), so reaching
a point at distance `d` from the start requires at least `d / max_step`
updates, and since the update count is an integer, at least
`ceil(d / max_step)` of them. `cb_calibration_min_steps` computes exactly this
quantity and is checked against `cb_calibration_step` under 1000 repeated
hostile-gradient updates in `tests/test_calibration.c` and 50000 randomized
configurations in `tests/test_property.c` (`prop_calibration_bounds`).

**Why this matters against slow poisoning:** an attacker who can bias the
*gradient* fed to calibration (for example by injecting many trusted-looking
readings over time) is still limited to `max_step` of influence per accepted
update, and every update is logged with a reason by the calibration layer
(outside this library's scope). The attacker cannot walk a threshold to an
arbitrary value in one step, and the number of updates required to reach any
specific target is a quantity the caller can compute in advance and treat as
a detection budget: if `cb_calibration_min_steps` says a meaningful move
needs 200 accepted updates, that is 200 opportunities for a human reviewer or
an anomaly check elsewhere in the system to notice a sustained one-directional
drift in a parameter that is supposed to only ever be nudged by genuine,
confirmed outcomes.

---

## 3. Why each algorithm, instead of something simpler

### 3.1 EWMA + CUSUM instead of a fixed threshold

A fixed absolute threshold needs a calibrated range for the quantity being
monitored. This library is domain-neutral and has no such range; the EWMA
gives each stream its own learned baseline and scale. A fixed threshold also
treats every sample independently, so a change too small to clear the
threshold on any single sample is invisible forever, however long it
persists. CUSUM accumulates signed deviations from the baseline, so a
sustained small shift eventually crosses the accumulated threshold even
though no individual sample would (Page, 1954; Basseville & Nikiforov, 1993,
*Detection of Abrupt Changes: Theory and Application* — the standard
reference this design follows for the two-sided CUSUM form and the
Siegmund/Lorden average-run-length approximations used in
`cb_detector_arl0`/`cb_detector_arl1`).

The optional baseline hold (`baseline_hold_enabled`,`baseline_hold_frac`)
exists because an EWMA that keeps adapting *absorbs* a sustained shift into
its own baseline — after enough samples the residual returns toward zero and
the shift that was supposed to be flagged disappears into "normal." Freezing
the baseline once the CUSUM has already accumulated meaningful evidence of a
shift keeps the shift visible through to the alarm. This tradeoff is
quantified directly in `docs/testing.md` (false-alarm rate measured with the
hold on vs. off).

**What this detector is not.** As corrected during development: this is a
*change-point* detector for a stream of measurements the caller already
considers trustworthy — it answers "does this stream show a sustained shift
consistent with a real change in the quantity being measured," never "is this
sensor lying" or "has this channel been compromised." Those are questions
about the authenticity of the channel, belong to a different layer entirely
(a trust/security watchdog that is not part of this library and is not even
approximated by it), and this library deliberately does not attempt, re-check,
or second-guess that judgment. See `docs/architecture.md` for the boundary
statement.

### 3.2 Log-odds fusion instead of, e.g., weighted averaging

Averaging probabilities (or raw scores) does not correspond to any coherent
model of combining independent evidence, and has no natural cap: an attacker
who can report an extreme score moves a weighted average by an amount that
depends on how many other scores exist, not on any principled bound. Log-odds
addition is the correct Bayesian combination rule for conditionally
independent evidence (each item's log-likelihood-ratio is additive in
log-odds space), and because it is additive, a per-item *and* per-group cap on
that log-contribution translates directly into a provable bound on the total
— which is exactly the fault-tolerance inequality in Section 1. This is the
central reason log-odds fusion was chosen over any ad hoc scoring scheme: the
cap composes.

### 3.3 Hungarian assignment instead of greedy matching

A greedy "take the best available pairing first" heuristic is not optimal in
general and, more importantly for a system whose decisions may be logged,
replayed, and audited, is **not stable under input reordering**: the same set
of rows and columns fed in a different order can produce a different total
cost under greedy matching. The assignment problem has an exact polynomial
solution (the Hungarian / Kuhn–Munkres algorithm, here in its
shortest-augmenting-path form with dual potentials, O(n² m)), so there is no
reason to approximate it, and the result is a pure function of the cost
matrix — independent of row or column order — which the property test
`prop_assignment_optimal` checks against randomly permuted verification, and
`test_against_brute_force` checks against independent brute-force enumeration
for small matrices.

### 3.4 Expected-cost decision rule instead of a fixed probability threshold

A single fixed probability threshold implicitly assumes a fixed ratio between
the cost of acting wrongly and the cost of not acting when needed. Making
that ratio an explicit cost table, and deriving the threshold from it
(`T* = (cost_act_when_false - cost_hold_when_false) / D`), makes the tradeoff
auditable and tunable without touching any code — exactly the "no illustrative
numeric constant baked into logic" constraint this project is built under. It
also generalizes cleanly to more than two actions (`cb_decision_ladder`)
without changing the underlying principle.

---

## 4. Correlation-graph floor: statement and proof

`citybrain/graph.h`'s central guarantee: **no "lowers" edge, and no
composition of "lowers" edges of any length, can produce an applied
multiplier below `m_min`.**

**Proof.** Every edge's multiplier is clipped to `[m_min, m_max]` at the
moment it is stored (`cb_graph_set_edge`). Composition
(`cb_graph_path_multiplier`) multiplies the *stored* (already-clipped) edge
values together and then clips the **product** to `[m_min, m_max]` again
before returning it. The second clip is unconditional — it runs regardless of
what the raw product evaluated to — so the returned value is a member of
`[m_min, m_max]` by construction, for a path of any length, including the
pathological case of every edge on the path being set to exactly `m_min`
(whose raw, unclipped product would be `m_min^(k-1)` for a `k`-node path,
arbitrarily close to zero as `k` grows, and is *not* what gets returned).
`tests/test_graph.c` (`test_floor_under_composition`) checks this for chains
up to the maximum supported graph size, and `tests/test_property.c`
(`prop_graph_bounds`) checks it over thousands of randomly generated graphs
and paths. `cb_graph_adjust_prior` applies the same clipped-composition value
to a prior's odds, so the same guarantee carries through to prior adjustment.

This is why a spoofed "lowers" signal cannot fully suppress a real,
independently-corroborated incident: the worst a correlation adjustment can
do is scale the prior odds down by `m_min`, which Section 1.7 shows costs the
real evidence at most a bounded, computable number of additional independent
groups — never an unbounded amount.

---

## 5. Related work

This section states plainly where the design is a direct application of
established results and where it is a deliberate simplification, and why
that simplification is acceptable here.

### 5.1 Change-point detection

- **Page (1954)**, "Continuous Inspection Schemes," introduced the CUSUM
  statistic. **Basseville & Nikiforov (1993)**, *Detection of Abrupt Changes:
  Theory and Application*, is the standard reference this design follows for
  the two-sided CUSUM form, the slack/threshold parameterization, and the
  average-run-length tradeoff between false-alarm rate and detection delay
  that `cb_detector_arl0`/`cb_detector_arl1` approximate (via Siegmund's
  corrected-Brownian-motion formula). This library is a **direct
  application** of that theory: the CUSUM recursion, the two-sided form, and
  the ARL approximations are the textbook versions, not a novel variant.
- **Murguia & Ruths (2017)**, "Characterization of Model-Based Detectors for
  CPS Sensor Faults/Attacks" (arXiv:1710.04407; also published work comparing
  CUSUM against a chi-squared static detector for cyber-physical systems),
  quantifies the advantage of a dynamic (CUSUM) detector that uses the
  history of the state over a static per-sample detector, and derives tuning
  procedures from a target false-alarm rate. The design choice to pair an
  adaptive EWMA baseline with CUSUM, rather than a static per-sample test,
  directly follows the finding in this literature that dynamic detectors
  catch gradual shifts a static test misses — this is the paper that
  motivated testing a slow sustained shift explicitly
  (`test_slow_ramp` / `prop_bound_is_tight`-adjacent scenarios) rather than
  relying only on a step-change test.
- **Deliberate simplification:** this library's detector is a *generic*
  univariate change-point detector operating on a scalar residual stream. The
  CPS literature above generally works with a full dynamical system model
  (state-space residuals from a Kalman filter or observer) and vector-valued
  CUSUM statistics. A general-purpose library cannot assume a caller has such
  a model; the scalar EWMA-normalized form is the simplification, and it is
  acceptable here because this library's job is narrower — detecting a
  sustained shift in an already-validated measurement stream, not performing
  full state estimation or fault isolation on a dynamical system.

### 5.2 Resilient / Byzantine-tolerant distributed detection and fusion

- **Marano, Matta & Tong (2009)**, "Distributed Detection in the Presence of
  Byzantine Attacks," *IEEE Transactions on Signal Processing* 57(1):16-29,
  and the related **Kailkhura, Han, Brahma & Varshney (2013/2014)**,
  "Distributed Bayesian Detection with Byzantine Data," study exactly the
  general question this library's fault-tolerance inequality answers for the
  single-cap case: how many corrupted/compromised sources can a fusion system
  tolerate before its guarantee breaks? Their central result is a **critical
  fraction** of compromised nodes (in the canonical binary-vote setting,
  `alpha = 0.5` of the reporting population) above which a fusion center is
  "blinded" — its achievable Kullback-Leibler divergence between hypotheses
  can be driven to zero, so no amount of additional (even infinite) honest
  evidence restores discriminating power, regardless of any single-item cap.
- **Where this design sits relative to that literature:** this library's
  bound is a **narrower, single-parameter special case**, not a general
  Byzantine-resilience result. It answers "how many groups does a bounded-cap
  adversary need to *reach a threshold*," not "what fraction of the total
  evidence population can be compromised before the system's discriminating
  power collapses entirely." The Byzantine-detection literature's "blinding"
  phenomenon is a structural property of *unbounded* influence per
  compromised vote; this design sidesteps it entirely by construction, by
  capping every single source's influence *before* fusion, so there is no
  configuration-dependent critical fraction to find — the worst case for any
  number of controlled groups `n` is always the explicit, computable
  `p_max(n)` from Section 1.3, and it never saturates to "indistinguishable
  from certainty" for a bounded `n` because `p_max` is a composition of
  finitely many finite steps.
- **Deliberate simplification:** this library does not model what happens
  when a *majority or near-majority* of all evidence groups are compromised
  simultaneously — that is a genuinely harder problem (robust statistics /
  Byzantine consensus over the *whole* evidence set), it depends on
  system-specific assumptions about how many total independent groups
  typically exist, and it is explicitly out of scope for a deterministic
  per-decision fusion primitive. The guarantee this library provides is
  deliberately the weaker, narrower one — "a small, bounded number of
  compromised groups cannot cross a threshold" — because that is the specific
  invariant (G3) the calling system needs, and it is provable exactly and
  cheaply, as opposed to the harder general question, which would require
  assumptions this library has no way to check.

### 5.3 Assignment and decision theory

- The **Hungarian algorithm** (Kuhn, 1955; Munkres, 1957) and
  **Bayes expected-cost decision rules** (standard statistical decision
  theory, e.g. Berger, *Statistical Decision Theory and Bayesian Analysis*)
  are both textbook results, applied here directly and without modification
  beyond the deterministic tie-breaking rules documented in
  `citybrain/assignment.h` and `citybrain/decision.h`, which are an
  engineering addition (needed for reproducibility/replay) rather than a
  mathematical one.

---

## 6. Incident physics: grounding likelihood ratios in domain physics

`citybrain/physics.h` is an optional, pluggable module that computes
likelihood ratios and expected physical quantities for `cb_fusion_posterior`
to consume as ordinary evidence, instead of relying solely on an arbitrarily
configured number. It does not modify, call into, or change the behaviour of
fusion, the cap, the correlation graph, or the cost-decision rule in any
way — it only ever produces a `likelihood_ratio` that a caller hands to
`cb_fusion_posterior` exactly as any other evidence item.

### 6.1 Scope, restated

Every function in this module answers: *"how consistent is this
already-trusted measurement with a real incident of this physical kind, at
some hypothesised severity, versus normal background conditions?"* The
answer is always a likelihood ratio — a measure of degree — never a
true/false verdict. No function here flags, rejects, or asserts that a
reading is untrustworthy; that determination belongs entirely to the
Guardian/trust layer, which is not part of this repository and is not
modelled or second-guessed here. This is the same boundary already drawn
for the change-point detector in Section 3.1 of this document, applied to a
second module.

### 6.2 The shared statistical primitive: Gaussian likelihood-ratio test

Every physical sub-model below produces an *expected* value of some physical
quantity under a hypothesised incident (`expected_h1`) and under normal
background conditions (`expected_h0`). Converting "how far is the observed
value from each" into a likelihood ratio is done by exactly one shared
function, `cb_physics_gaussian_likelihood_ratio`, implementing the
closed-form likelihood ratio between two simple Gaussian hypotheses with
known means and a shared standard deviation:

```
LR = exp( [ (observed - expected_h0)^2 - (observed - expected_h1)^2 ]
          / (2 * sigma^2) )
```

This is the Neyman-Pearson (1933) likelihood ratio for this exact case; see
S. M. Kay, *Fundamentals of Statistical Signal Processing, Volume II:
Detection Theory*, Prentice Hall, 1998, chapter 3, for the standard
textbook derivation and closed form used here. Implementing this once and
having every physical model call it, rather than re-deriving the same
algebra three times, is the same single-implementation discipline the rest
of this library follows (`docs/architecture.md`, "No algorithm implemented
more than once").

**A documented limitation, found and kept rather than hidden.** This
closed form compares an observation's *relative* distance to exactly two
point hypotheses; it does not independently ask "is this plausible under H1
at all?" Concretely: with `expected_h0 = 0`, `expected_h1 = 1.3436`
(an ultrafast fire's plume-predicted concentration at 300s), `sigma = 0.269`,
an observed value of `1,000,000` — far beyond anything any real fire could
produce at that sensor — still yields `LR ≈ +inf`, strongly *favouring* the
fire hypothesis, because `1,000,000` is marginally closer (in squared
distance) to `1.3436` than to `0`. A domain expert would call this reading
absurd under H1 too, but the two-point Gaussian LRT has no way to express
that without a bounded or heavier-tailed H1 distribution, which is out of
scope for this minimal implementation. `tests/test_adversarial.c`
(`adversary_physics_absurd_readings`) and the matching Python test
(`test_physics_absurd_readings_are_low_evidence_not_errors` /
`test_physics_gaussian_lrt_never_crashes_or_nans`) both exercise this
directly: the function never crashes or returns NaN, but is honestly *not*
claimed to catch every implausible-under-H1 case on its own. The fusion
layer's cap (Section 1) is what ultimately bounds any single evidence
item's influence regardless of how this number was produced — which is
exactly why that cap exists upstream of every evidence source, physics-derived
or otherwise.

### 6.3 Flood: shallow-reservoir mass balance

`cb_physics_flood_rise_rate` implements continuity (mass balance) for a
zone modelled as a shallow, well-mixed reservoir:

```
dV/dt = Q_in - Q_out(h)      V = A * h  (well-mixed, constant surface area A)
=>  dh/dt = (Q_in - Q_out(h)) / A
```

**Source**: D. R. Maidment (ed.), *Handbook of Hydrology*, McGraw-Hill,
1993, chapter 10 ("Reservoir and River Routing"), for the continuity
relation; V. T. Chow, *Open-Channel Hydraulics*, McGraw-Hill, 1959, chapter
14, for the power-law stage-discharge outflow relation
`Q_out(h) = drainage_coefficient * h^drainage_exponent` (exponent 1.5 for a
broad-crested weir/rectangular spillway, 0.5 for a submerged orifice,
derivable from Bernoulli's equation plus the critical-flow condition).

**Simplifying assumptions and where they stop holding**:
- the zone is well-mixed with one representative water level — invalid for
  a large or channelised zone with markedly non-uniform rise;
- inflow is treated as constant over the modelled timestep;
- the stage-discharge relation is a single power law across the whole range
  of `h`, which real outlet structures only approximate locally, and which
  real structures with multiple outlet types (e.g. an orifice that
  transitions to weir flow once submerged) do not follow at all.

**Worked example**, cross-checked independently in Python (`gen_vectors.py`)
against the compiled C function: area 1000 m², inflow 5 m³/s, drainage
coefficient 0.5, exponent 1.5 (weir-like), water level 2 m →
`outflow = 0.5 * 2^1.5 = 1.4142135623730951`, `rate = (5 - 1.4142...) / 1000
= 0.0035857864376269049` — bit-identical between the Decimal-precision
generator and the compiled `double` function.

### 6.4 Fire: t-squared growth + Gaussian plume dispersion

**t-squared growth.** `cb_physics_fire_heat_release_rate` implements
`Q(t) = alpha * t^2`, the standard fire-growth model for the free growth
phase of a fire. **Source**: NFPA 72, *National Fire Alarm and Signaling
Code*, Annex B; reproduced with the same published coefficients in B.
Karlsson and J. G. Quintiere, *Enclosure Fire Dynamics*, CRC Press, 2000,
table 1.1, and the SFPE *Handbook of Fire Protection Engineering*. The four
published growth classes (`cb_physics_fire_growth_class_alpha`):

| class | alpha (kW/s²) | time to 1 MW |
|---|---|---|
| slow | 0.00293 | ~600 s |
| medium | 0.0117 | ~300 s |
| fast | 0.0469 | ~150 s |
| ultrafast | 0.1876 | ~75 s |

These are exposed as a convenience lookup only — `cb_physics_fire_heat_release_rate`
itself takes `alpha` as an ordinary parameter and has no awareness the table
exists, the same pattern `cb_graph`'s example profile and `cb_detector`'s
illustrative defaults already follow.

**Simplifying assumption**: this models the free growth phase only — not
flashover, a ventilation- or fuel-limited plateau, or decay, which are
separate, well-documented phases in the same literature (Karlsson &
Quintiere, chapter 1) this model does not attempt to cover.

**Gaussian plume dispersion.** `cb_physics_plume_concentration` implements
the standard ground-reflected Gaussian plume model:

```
C = Q / (2*pi*u*sigma_y*sigma_z)
    * exp(-y^2 / (2*sigma_y^2))
    * [ exp(-(z-H)^2 / (2*sigma_z^2)) + exp(-(z+H)^2 / (2*sigma_z^2)) ]
```

**Source**: D. B. Turner, *Workbook of Atmospheric Dispersion Estimates*,
U.S. EPA, 1970 — the standard reference for this exact closed form and for
computing `sigma_y`/`sigma_z` from downwind distance and a Pasquill-Gifford
atmospheric stability class.

**Simplifying assumptions and known limitations**:
- `sigma_y`/`sigma_z` are taken as direct parameters rather than derived
  from distance and a stability class — this function implements the plume
  equation itself, not Turner's full empirical dispersion-coefficient
  parameterisation, which is left to the caller (or a future extension);
- the model is undefined in calm-wind conditions (`u -> 0`), a well-known
  limitation in the dispersion literature itself, not an artefact of this
  implementation — `cb_physics_plume_concentration` returns `CB_ERR_DOMAIN`
  for `wind_speed_m_s <= 0` for exactly this reason;
- no plume rise, deposition, or chemical transformation is modelled.

**Worked example**: ground-level centerline (`y=0, z=0, H=0`), emission
rate 10, wind speed 2 m/s, `sigma_y=50, sigma_z=20` →
`C = (10 / (2*pi*2*50*20)) * 1 * 2 = 0.0015915494309189535`, bit-identical
between the Decimal generator and the compiled function.

### 6.5 Traffic: Greenshields speed-density relation

`cb_physics_traffic_greenshields_speed` and `..._flow` implement
Greenshields' linear speed-density relation:

```
v(k) = v_free * (1 - k / k_jam)         q(k) = k * v(k)
```

**Source**: B. D. Greenshields, "A Study of Traffic Capacity," *Highway
Research Board Proceedings*, vol. 14, 1935, pp. 448-477 — the founding
empirical model of the traffic fundamental diagram, still a standard
baseline in traffic flow theory.

**Underlying continuum theory, noted but not implemented**: Greenshields'
algebraic relation is the equilibrium fundamental-diagram closure for the
Lighthill-Whitham-Richards (LWR) kinematic-wave model,
`∂k/∂t + ∂q(k)/∂x = 0` (M. J. Lighthill and G. B. Whitham, *Proc. Royal
Society A*, 1955; P. I. Richards, *Operations Research*, 1956). LWR
describes how a traffic state propagates and evolves in space and time;
this library implements only the static, equilibrium speed-density
relation, not the time-dependent PDE. This is why these functions
characterise consistency with a *sustained* congestion state (the design
brief's own "jam" vocabulary, as an example) rather than the dynamics of
how quickly a flow can plausibly change — modelling an "accident"'s
*instantaneous* flow drop as a dynamic event would require the fuller LWR
PDE, which is explicitly out of scope here (noted per the task's own
phrasing, not implemented).

**Simplifying assumption**: the model is only physically meaningful for
`0 <= density <= jam_density`. Outside that range the formula is evaluated
exactly as given rather than rejected or clamped — `traffic_greenshields_speed(30, 120, 150)`
returns `-7.5`, a negative "expected speed" that is itself the evidentiary
signal `cb_physics_gaussian_likelihood_ratio` is built to weigh, not a
condition this function special-cases.

**Worked example**: `v_free=30, k_jam=120, k=60` → `v = 30*(1-60/120) = 15`,
`q = 60*15 = 900`; `k=30` → `v=22.5, q=675` — both cross-checked against
the compiled function and the Decimal-precision generator.

### 6.6 Cross-check against the compiled code

Every numeric claim in this section (the worked examples in 6.3-6.5, and
the limitation example in 6.2) was computed three independent ways before
being committed: by hand in Python, by the arbitrary-precision Decimal
generator in `tests/vectors/gen_vectors.py` (sections `physics_gaussian_lrt`,
`physics_flood`, `physics_fire_hrr`, `physics_plume`, `physics_traffic`),
and by the compiled C functions via `tests/test_physics.c` and
`tests/test_vectors.c`. All three agree to the tolerances stated in
`docs/testing.md`. No discrepancy was found during this cross-check (unlike
Section 1.6's fault-tolerance cross-check, which did catch a real bug) —
stated here explicitly rather than left silent, consistent with this
project's practice of reporting a clean cross-check result just as plainly
as one that found something.

## 7. Summary of what is proved vs. assumed

| statement | status |
|---|---|
| A single capped group cannot reach threshold `T` alone if `n_min(c, p0, T) > 1` | **Proved** (Section 1.4), cross-checked against the worked example (Section 1.6), and checked by property test across tens of thousands of random configurations |
| `n_min` independent groups are necessary and sufficient to reach `T` in the worst case | **Proved** (`n_min` is defined as exactly this threshold crossing point; tightness checked in `tests/test_adversarial.c` and `prop_bound_is_tight`) |
| A correlation-graph "lowers" composition cannot suppress below `m_min` | **Proved** (Section 4), checked for chains and randomized graphs |
| A calibration update cannot leave `[lo, hi]` or move faster than `max_step`/update | **Proved** (Section 2), checked under 1000 hostile updates and 50000 random configurations |
| Two evidence items assigned different `group_id`s are actually independent | **Assumed** — this is a pipeline/integration responsibility, not something this library can verify (Section 1.5) |
| A change-point alarm reflects a real shift in the underlying quantity rather than a compromised channel | **Assumed** — authenticity/trust judgments are explicitly out of scope for this library (Section 3.1) |
| A physics-derived likelihood ratio (Section 6) is bounded by the same fusion cap as any other evidence item, with no special exemption | **Proved by construction** (Section 1.3: the cap is applied in `cb_fusion_posterior` to every item regardless of origin) and demonstrated in code (`test_physics_feeds_fusion_unmodified`) |
| The Gaussian LRT (Section 6.2) independently recognises a value that is implausible under H1 itself, not just relative to H0 | **Not claimed** — a documented limitation (Section 6.2), demonstrated directly in `adversary_physics_absurd_readings` |
