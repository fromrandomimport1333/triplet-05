# API reference

One entry per public function: C signature, Python signature, parameters,
return/errors, and a worked numeric example. Grouped by module, matching
`include/citybrain/*.h`.

**Agnosticism note (applies to every function below that takes a weight):**
this library is agnostic to how a trust/confidence weight was produced.
Mapping any external verdict scheme (a classifier score, a sensor health
metric, a categorical label from another system) onto a `[0, 1]` weight is
the integrating application's responsibility, not this library's — no
function here inspects, validates, or knows about such a scheme.

Every C function returns `cb_status_t`; `CB_OK == 0` is success, any other
value is a documented error and leaves output arguments untouched unless
stated otherwise. Every Python wrapper raises a subclass of
`citybrain.errors.CityBrainError` instead of returning that code — see
`citybrain/errors.py` for the exact mapping (`CB_ERR_NULL` →
`NullArgumentError`, `CB_ERR_DOMAIN` → `DomainError`, etc).

---

## common.h

### `cb_status_message`
```c
cb_status_t cb_status_message(cb_status_t status, const char **out_message);
```
No direct Python equivalent (the Python layer never surfaces a raw status
code — see `errors.check`). Returns a human-readable string for any status
code, including unrecognized ones. Total function; cannot fail except
`CB_ERR_NULL` if `out_message` is NULL.

### `cb_version_string` / `cb_version_numbers`
```c
cb_status_t cb_version_string(const char **out_text);
cb_status_t cb_version_numbers(int32_t *out_major, int32_t *out_minor, int32_t *out_patch);
```
Python: `citybrain.common.version_string() -> str`,
`citybrain.common.version_numbers() -> tuple[int, int, int]`.
Example: `citybrain.common.version_string()` → `"1.1.0"`.

---

## fusion.h

### `cb_fusion_config_init`
```c
cb_status_t cb_fusion_config_init(cb_fusion_config_t *cfg);
```
Python: `citybrain.fusion.default_config() -> FusionConfig`.
Fills illustrative defaults: `lr_cap=8.0, prior=0.02, posterior_floor=0.0,
posterior_ceiling=1.0, group_cap_enabled=True`. Errors: `CB_ERR_NULL`.

### `cb_fusion_posterior`
```c
cb_status_t cb_fusion_posterior(const cb_fusion_config_t *cfg,
    const cb_evidence_t *items, size_t n_items,
    double *group_scratch, size_t n_groups, cb_fusion_result_t *out);
```
Python: `citybrain.fusion.fuse(config: FusionConfig, items: Sequence[Evidence]) -> FusionResult`
(the Python wrapper allocates and sizes `group_scratch` itself).

Fuses `n_items` evidence items into a posterior, applying the per-item and
per-group likelihood-ratio cap. `group_scratch` needs >= `n_groups` doubles
when group capping is enabled.

Errors: `CB_ERR_NULL` (`cfg`/`out` NULL, or `items` NULL with `n_items>0`, or
group capping on and `group_scratch` NULL) → `NullArgumentError`;
`CB_ERR_DOMAIN` (`lr_cap<=1`; `prior` outside `(0,1)`; floor/ceiling
inconsistent; `weight` outside `[0,1]`; `likelihood_ratio<=0`; `group_id`
out of range) → `DomainError`; `CB_ERR_NOT_FINITE` → `NotFiniteError`;
`CB_ERR_CAPACITY` (`n_items`/`n_groups` too large) → `CapacityError`.

Example (design brief worked example):
```python
cfg = citybrain.fusion.FusionConfig(lr_cap=8.0, prior=0.02)
r = citybrain.fusion.fuse(cfg, [citybrain.fusion.Evidence(1e300, 1.0, 0)])
r.posterior  # 0.1403508771929825
```

### `cb_fusion_posterior_independent`
```c
cb_status_t cb_fusion_posterior_independent(const cb_fusion_config_t *cfg,
    const cb_evidence_t *items, size_t n_items, cb_fusion_result_t *out);
```
Python: `citybrain.fusion.fuse_independent(config, items) -> FusionResult`.
Convenience form: every item is its own group. Same error codes as above
minus the group-id cases.

### `cb_logit` / `cb_logistic`
```c
cb_status_t cb_logit(double probability, double *out_log_odds);
cb_status_t cb_logistic(double log_odds, double *out_probability);
```
Python: `citybrain.fusion.logit(p) -> float`, `citybrain.fusion.logistic(L) -> float`.
`logit(0.5) == 0.0`. `cb_logit` errors `CB_ERR_DOMAIN` for `p` outside
`(0,1)`; `cb_logistic` never errors on domain (saturates cleanly for large
`|L|`) but errors `CB_ERR_NOT_FINITE` for a non-finite `L`.

### `cb_ft_params_init`
```c
cb_status_t cb_ft_params_init(cb_ft_params_t *params);
```
Python: `citybrain.fusion.default_ft_params() -> FaultToleranceParams`.
Defaults: `lr_cap=8.0, prior=0.02, threshold=0.20, max_weight=1.0,
odds_multiplier=1.0` (the design brief's own worked example).

### `cb_ft_min_sources`
```c
cb_status_t cb_ft_min_sources(const cb_ft_params_t *params, int32_t *out_min_sources);
```
Python: `citybrain.fusion.min_sources(params: FaultToleranceParams) -> int`.
See `docs/math_reasoning.md` §1 for the derivation. Errors: `CB_ERR_NULL`,
`CB_ERR_DOMAIN`, `CB_ERR_NOT_FINITE`, `CB_ERR_CAPACITY` (if the required
count would exceed `CB_FT_MAX_SOURCES`).

Example: `min_sources(FaultToleranceParams(lr_cap=8.0, prior=0.02,
threshold=0.20))` → `2`.

### `cb_ft_max_posterior`
```c
cb_status_t cb_ft_max_posterior(const cb_ft_params_t *params, int32_t n_sources, double *out_posterior);
```
Python: `citybrain.fusion.max_posterior(params, n_sources: int) -> float`.
Example: `max_posterior(params, 1)` → `0.1403508771929825`;
`max_posterior(params, 2)` → `0.5663716814159292`.

### `cb_ft_check_invariant`
```c
cb_status_t cb_ft_check_invariant(const cb_ft_params_t *params, int32_t n_controlled, int32_t *out_safe);
```
Python: `citybrain.fusion.check_invariant(params, n_controlled: int) -> bool`.
`True` means `n_controlled` capped groups, acting alone, cannot reach
`params.threshold` (the G3 invariant). Example:
`check_invariant(params, 1)` → `True`; `check_invariant(params, 2)` → `False`.

---

## detector.h

### `cb_detector_config_init` / `cb_detector_config_validate`
```c
cb_status_t cb_detector_config_init(cb_detector_config_t *cfg);
cb_status_t cb_detector_config_validate(const cb_detector_config_t *cfg);
```
Python: `citybrain.detector.default_config() -> DetectorConfig` (validation
happens implicitly inside every call that takes a config).
Defaults: `ewma_alpha=0.10, var_alpha=0.05, min_sigma=1e-6, cusum_slack=0.5,
cusum_threshold=5.0, baseline_hold_enabled=True, baseline_hold_frac=0.5,
warmup_samples=20`.

### `cb_detector_init`
```c
cb_status_t cb_detector_init(cb_detector_state_t *state, const cb_detector_config_t *cfg,
    double first_value, double initial_sigma);
```
Python: `citybrain.detector.Detector(config, first_value, initial_sigma)`
(constructor). `initial_sigma` must be `> 0`. Errors: `CB_ERR_DOMAIN` if
`initial_sigma<=0`; `CB_ERR_NOT_FINITE`.

### `cb_detector_update`
```c
cb_status_t cb_detector_update(cb_detector_state_t *state, const cb_detector_config_t *cfg,
    double value, cb_detector_output_t *out);
```
Python: `Detector.update(value: float) -> DetectorOutput`.
Feeds one measurement; returns residual, CUSUM accumulators, and alarm
flags (`.warmup`, `.shift_up`, `.shift_down`, `.baseline_held` as
`DetectorOutput` properties). Errors: `CB_ERR_STATE` if the detector was
not initialised; `CB_ERR_NOT_FINITE` for a NaN/infinite `value`.

Example: feeding a 3-sigma sustained step after 200 stationary samples
raises `.shift_up` within ~10-15 samples at the default `(k=0.5, h=5)`.

### `cb_detector_reset_alarms`
```c
cb_status_t cb_detector_reset_alarms(cb_detector_state_t *state);
```
Python: `Detector.reset_alarms() -> None`. Clears both CUSUM accumulators,
keeps the learned baseline.

### `cb_detector_baseline`
```c
cb_status_t cb_detector_baseline(const cb_detector_state_t *state, const cb_detector_config_t *cfg,
    double *out_baseline, double *out_sigma);
```
Python: `Detector.baseline() -> tuple[float, float]`. Reads current
baseline/scale without feeding a sample.

### `cb_detector_arl0` / `cb_detector_arl1`
```c
cb_status_t cb_detector_arl0(double cusum_slack, double cusum_threshold, double *out_arl0);
cb_status_t cb_detector_arl1(double cusum_slack, double cusum_threshold, double shift_sigmas, double *out_arl1);
```
Python: `citybrain.detector.arl0(slack, threshold) -> float`,
`citybrain.detector.arl1(slack, threshold, shift_sigmas) -> float`.
Siegmund approximations for average run length to false alarm / detection
delay. `arl1` errors `CB_ERR_DOMAIN` when `shift_sigmas <= cusum_slack` (the
shift is inside the slack band; the delay is unbounded).

Example: `arl0(0.5, 5.0)` ≈ `938.2`; `arl1(0.5, 5.0, 1.0)` ≈ `10.34`.

---

## graph.h

### `cb_graph_config_init`
```c
cb_status_t cb_graph_config_init(cb_graph_config_t *cfg);
```
Python: `citybrain.graph.default_config() -> GraphConfig`.
Defaults: `m_min=0.5, m_max=3.0, n_nodes=4`.

### `cb_graph_init`
```c
cb_status_t cb_graph_init(cb_graph_t *graph, const cb_graph_config_t *cfg);
```
Python: `citybrain.graph.Graph(config: GraphConfig)` (constructor).
Errors: `CB_ERR_DOMAIN` for `m_min` outside `(0,1]`, `m_max<1`, or `n_nodes`
outside `[1, CB_GRAPH_MAX_NODES]` (8).

### `cb_graph_set_edge` / `cb_graph_get_edge` / `cb_graph_clear_edge`
```c
cb_status_t cb_graph_set_edge(cb_graph_t *graph, int32_t from, int32_t to, cb_edge_kind_t kind, double multiplier);
cb_status_t cb_graph_get_edge(const cb_graph_t *graph, int32_t from, int32_t to, cb_edge_kind_t *out_kind, double *out_multiplier);
cb_status_t cb_graph_clear_edge(cb_graph_t *graph, int32_t from, int32_t to);
```
Python: `Graph.set_edge(from_node, to_node, kind: EdgeKind, multiplier)`,
`Graph.get_edge(from_node, to_node) -> tuple[EdgeKind, float]`,
`Graph.clear_edge(from_node, to_node)`.
`set_edge` clips `multiplier` into `[m_min, m_max]` before storing it.
Errors: `CB_ERR_DOMAIN` for a self-loop (`from==to`), out-of-range index, or
`kind`/`multiplier` sign mismatch (`RAISES` needs `>=1`, `LOWERS` needs
`<=1`).

Example: `set_edge(0, 1, EdgeKind.LOWERS, 1e-9)` then `get_edge(0, 1)` →
`(EdgeKind.LOWERS, 0.5)` (clipped to `m_min`).

### `cb_graph_path_multiplier`
```c
cb_status_t cb_graph_path_multiplier(const cb_graph_t *graph, const int32_t *path, size_t path_len, double *out_multiplier);
```
Python: `Graph.path_multiplier(path: Sequence[int]) -> float`.
Composes stored edge multipliers along `path` and clips the **product** to
`[m_min, m_max]` — this is the function the composition-floor proof is
about (`docs/math_reasoning.md` §4).

Example: 7 chained `LOWERS` edges at `0.5` each, `m_min=0.5` →
`path_multiplier` returns `0.5`, not `0.5^7 ≈ 0.0078`.

### `cb_graph_extremal_multiplier`
```c
cb_status_t cb_graph_extremal_multiplier(const cb_graph_t *graph, int32_t from, int32_t to, double *out_min, double *out_max);
```
Python: `Graph.extremal_multiplier(from_node, to_node) -> tuple[float, float]`.
Min/max composed multiplier over all simple paths. Errors:
`CB_ERR_NOT_FOUND` if no path exists (outputs still set to `1.0`).

### `cb_graph_adjust_prior`
```c
cb_status_t cb_graph_adjust_prior(const cb_graph_t *graph, int32_t target,
    const int32_t *active, size_t n_active, double prior,
    double *out_adjusted_prior, double *out_multiplier);
```
Python: `Graph.adjust_prior(target, active: Sequence[int], prior) -> tuple[float, float]`.
Applies the product of direct edges from each distinct active category to
`target`, clipped to `[m_min, m_max]`, to `prior`'s odds. Order of `active`
does not affect the result.

Example: `adjust_prior(target=FIRE, active=[FLOOD], prior=0.02)` →
`(0.0101..., 0.5)`.

### `cb_graph_has_unrelated_pair`
```c
cb_status_t cb_graph_has_unrelated_pair(const cb_graph_t *graph, const int32_t *active, size_t n_active, int32_t *out_unrelated);
```
Python: `Graph.has_unrelated_pair(active: Sequence[int]) -> bool`.
Structural query only — see `docs/architecture.md` for why this does not
judge trustworthiness.

### `cb_example_load_graph` (example_profile.h)
```c
cb_status_t cb_example_load_graph(cb_graph_t *graph);
```
Python: `citybrain.example_profile.load_example_graph(graph: Graph) -> None`.
Installs one worked demonstration edge set. Sample data, not part of the
generic API (see `docs/architecture.md`, "What this component is NOT
responsible for").

---

## decision.h

### `cb_cost_table_init`
```c
cb_status_t cb_cost_table_init(cb_cost_table_t *table);
```
Python: `citybrain.decision.default_cost_table() -> CostTable`.
Defaults: `cost_act_when_false=1.0, cost_hold_when_true=20.0,
cost_act_when_true=0.0, cost_hold_when_false=0.0` (indifference threshold
`1/21 ≈ 0.0476`).

### `cb_decision_expected_cost`
```c
cb_status_t cb_decision_expected_cost(const cb_cost_table_t *table, double posterior, cb_decision_result_t *out);
```
Python: `citybrain.decision.expected_cost(table: CostTable, posterior: float) -> DecisionResult`.
Returns the lower-expected-cost action (`ACT`/`HOLD`); an exact tie resolves
to `HOLD`. Errors: `CB_ERR_DOMAIN` if `posterior` outside `[0,1]`.

Example: `expected_cost(default_cost_table(), 0.25)` →
`action=ACT, expected_cost_act=0.75, expected_cost_hold=5.0`.

### `cb_decision_ladder`
```c
cb_status_t cb_decision_ladder(const cb_action_cost_t *actions, size_t n_actions,
    double posterior, size_t *out_index, double *out_expected_cost);
```
Python: `citybrain.decision.ladder(actions: Sequence[ActionCost], posterior: float) -> tuple[int, float]`.
K-action generalisation; ties go to the lowest index (order the ladder
most-passive-first). Errors: `CB_ERR_DOMAIN` for `n_actions==0`;
`CB_ERR_CAPACITY` for `n_actions > CB_DECISION_MAX_ACTIONS` (4096).

---

## assignment.h

### `cb_assign_params_init`
```c
cb_status_t cb_assign_params_init(cb_assign_params_t *params);
```
No direct Python equivalent — `citybrain.assignment.solve()` builds the
params struct internally.

### `cb_assign_scratch_bytes`
```c
cb_status_t cb_assign_scratch_bytes(int32_t n_rows, int32_t n_cols, size_t *out_bytes);
```
Called internally by the Python wrapper; not exposed directly (Python
allocates its own scratch per call).

### `cb_assign_solve`
```c
cb_status_t cb_assign_solve(const cb_assign_params_t *params, int32_t *out_assignment,
    double *out_total, void *scratch, size_t scratch_bytes);
```
Python: `citybrain.assignment.solve(matrix: Sequence[Sequence[float]], maximize=False, infeasible_at=None) -> tuple[list[int], float]`.
Hungarian algorithm; handles rectangular matrices in either direction.
`assignment[i] == -1` means row `i` is left unassigned (only possible when
rows > columns). Errors: `CB_ERR_NO_SOLUTION` → `NoSolutionError` if every
complete assignment would need a pair marked infeasible;
`CB_ERR_CAPACITY` for dimensions `> CB_ASSIGN_MAX_DIM` (2048).

Example: `solve([[4,1,3],[2,0,5],[3,2,2]])` → `([1, 0, 2], 5.0)`.

---

## cache.h

### `cb_cache_bucket`
```c
cb_status_t cb_cache_bucket(const double *edges, size_t n_edges, double value, int32_t *out_bucket);
```
Python: `citybrain.cache.bucket(edges: Sequence[float], value: float) -> int`.
Half-open buckets from strictly increasing `edges`; `n` edges give `n+1`
buckets. Errors: `CB_ERR_DOMAIN` if `edges` is empty or not strictly
increasing.

### `cb_cache_key_make`
```c
cb_status_t cb_cache_key_make(int32_t category_id, int32_t partition_id, int32_t bucket_id,
    uint64_t state_version, cb_cache_key_t *out_key);
```
Python: `citybrain.cache.make_key(category_id, partition_id, bucket_id, state_version) -> CacheKey`.
Deterministic FNV-1a 64 hash over the four fields — stable across runs,
builds, and platforms, but **not a security primitive**.

Example: `make_key(0, 0, 0, 0).hash == 901300984310592933`.

### `cb_cache_key_equal`
```c
cb_status_t cb_cache_key_equal(const cb_cache_key_t *a, const cb_cache_key_t *b, int32_t *out_equal);
```
Python: `citybrain.cache.keys_equal(a: CacheKey, b: CacheKey) -> bool`.
Field-wise equality, never hash-wise — a hash collision is never read as a
cache hit.

### `cb_cache_key_valid`
```c
cb_status_t cb_cache_key_valid(const cb_cache_key_t *key, uint64_t current_state_version,
    uint64_t created_ms, uint64_t now_ms, uint64_t ttl_ms, int32_t *out_valid, uint32_t *out_reason);
```
Python: `citybrain.cache.key_valid(key, current_state_version, created_ms, now_ms, ttl_ms) -> Validity`
(`Validity` is a `NamedTuple(valid: bool, reason: int)`).
A key is valid only if `state_version` matches and age `< ttl_ms`. A
backwards-stepping clock cannot extend an entry's life (age clamps to 0).

---

## calibration.h

### `cb_calibration_bounds_init`
```c
cb_status_t cb_calibration_bounds_init(cb_calibration_bounds_t *bounds);
```
Python: `citybrain.calibration.default_bounds() -> CalibrationBounds`.
Defaults: `lo=0.0, hi=1.0, max_step=0.05, rate=0.1`.

### `cb_calibration_step`
```c
cb_status_t cb_calibration_step(const cb_calibration_bounds_t *bounds, double current, double gradient, cb_calibration_result_t *out);
```
Python: `citybrain.calibration.step(bounds, current, gradient) -> CalibrationResult`.
`theta_next = clip(current + clip(rate*gradient, -max_step, max_step), lo, hi)`.
Errors: `CB_ERR_DOMAIN` if `current` is outside `[lo, hi]` already.

Example: `step(default_bounds(), 0.5, 1e9)` → `value=0.55, step_clipped=True`.

### `cb_calibration_min_steps`
```c
cb_status_t cb_calibration_min_steps(const cb_calibration_bounds_t *bounds, double from, double to, int32_t *out_steps);
```
Python: `citybrain.calibration.min_steps(bounds, from_value, to_value) -> int`.
`ceil(|to-from| / max_step)`. Errors: `CB_ERR_DOMAIN` if `max_step==0` and
`from != to` (the move is impossible).

---

## mdp.h

### `cb_mdp_problem_init`
```c
cb_status_t cb_mdp_problem_init(cb_mdp_problem_t *problem);
```
No direct Python equivalent — `citybrain.mdp.solve()` builds the problem
struct from its arguments. Defaults: `discount=0.95, tolerance=1e-10,
max_iterations=10000, row_sum_tolerance=1e-9`.

### `cb_mdp_scratch_doubles`
```c
cb_status_t cb_mdp_scratch_doubles(int32_t n_states, size_t *out_doubles);
```
Called internally by the Python wrapper.

### `cb_mdp_solve`
```c
cb_status_t cb_mdp_solve(const cb_mdp_problem_t *problem, int32_t *out_policy, double *out_value,
    double *scratch, size_t scratch_doubles, int32_t *out_iterations);
```
Python: `citybrain.mdp.solve(transition, reward, discount=0.95, tolerance=1e-10, max_iterations=10000, row_sum_tolerance=1e-9) -> MDPSolution`.
Value iteration; `MDPSolution.converged` is `False` if `CB_ERR_NO_CONVERGE`
was returned (the policy/value from the last iteration is still usable).
Errors: `CB_ERR_DOMAIN` for a transition row not summing to 1 (within
`row_sum_tolerance`) or a negative probability; `CB_ERR_CAPACITY` for
`n_states > CB_MDP_MAX_STATES` (4096) or `n_actions > CB_MDP_MAX_ACTIONS` (64).

### `cb_mdp_state_index` / `cb_mdp_state_decode` / `cb_mdp_state_count`
```c
cb_status_t cb_mdp_state_index(const int32_t *dims, const int32_t *coords, size_t n_dims, int32_t *out_index);
cb_status_t cb_mdp_state_decode(const int32_t *dims, size_t n_dims, int32_t index, int32_t *out_coords);
cb_status_t cb_mdp_state_count(const int32_t *dims, size_t n_dims, int32_t *out_count);
```
Python: `citybrain.mdp.state_index(dims, coords) -> int`,
`citybrain.mdp.state_decode(dims, index) -> list[int]`,
`citybrain.mdp.state_count(dims) -> int`.
Mixed-radix index, row-major, last dimension fastest. `n_dims` up to
`CB_MDP_MAX_DIMS` (16).

Example: `state_index([4,4,8], [1,0,0])` → `32`.

---

## physics.h (optional, pluggable)

Every function below answers "how consistent is this already-trusted
measurement with a real incident of this physical kind" and returns a
likelihood ratio or an expected physical quantity — never a validity
verdict. This module is entirely optional, does not modify any other
module's behaviour, and the same agnosticism note at the top of this
document applies with one addition specific to this module: **the model
parameters here (areas, rates, dispersion coefficients, free-flow speeds)
describe physical quantities, not trust or verdict concepts — but a
function here is still never the right place to decide whether a reading
should be believed.** See `docs/math_reasoning.md`, "Incident physics," for
full derivations, cited sources, and documented simplifying assumptions.

### `cb_physics_gaussian_likelihood_ratio`
```c
cb_status_t cb_physics_gaussian_likelihood_ratio(double observed, double expected_h1,
    double expected_h0, double sigma, double *out_likelihood_ratio);
```
Python: `citybrain.physics.gaussian_likelihood_ratio(observed, expected_h1, expected_h0, sigma) -> float`.
Closed-form Gaussian likelihood-ratio test (Neyman & Pearson 1933; S. M.
Kay, *Fundamentals of Statistical Signal Processing, Vol. II: Detection
Theory*, 1998, ch. 3). The one statistical primitive every other function in
this module builds on. Never errors on an implausible `observed` value — see
the documented limitation below.

Errors: `CB_ERR_NULL`; `CB_ERR_NOT_FINITE`; `CB_ERR_DOMAIN` for `sigma <= 0`.

Example: `gaussian_likelihood_ratio(5.0, 8.0, 0.0, 2.0)` → `7.38905609893065`.

**Documented limitation**: for an `observed` value enormously far from both
`expected_h1` and `expected_h0` but marginally closer to `expected_h1`, the
ratio swings toward strongly favouring H1 — it does not independently
recognise "too extreme to be real under H1 itself." See
`docs/math_reasoning.md` for the worked example and why this is inherent to
comparing exactly two point hypotheses, not a bug.

### `cb_physics_flood_rise_rate`
```c
cb_status_t cb_physics_flood_rise_rate(double area_m2, double inflow_m3_per_s,
    double drainage_coefficient, double drainage_exponent, double water_level_m,
    double *out_rate_m_per_s);
```
Python: `citybrain.physics.flood_rise_rate(area_m2, inflow_m3_per_s, drainage_coefficient, drainage_exponent, water_level_m) -> float`.
Shallow-reservoir mass balance (Maidment, ed., *Handbook of Hydrology*,
1993, ch. 10) with a power-law stage-discharge outflow (Chow, *Open-Channel
Hydraulics*, 1959, ch. 14).

Errors: `CB_ERR_NULL`; `CB_ERR_NOT_FINITE`; `CB_ERR_DOMAIN` for
`area_m2 <= 0`, `drainage_coefficient < 0`, `drainage_exponent <= 0`, or
`water_level_m < 0` (the model's domain, not a trust judgement — see the
header comment).

Example: `flood_rise_rate(1000.0, 5.0, 0.5, 1.5, 2.0)` → `0.0035857864376269049`.

### `cb_physics_fire_growth_class_alpha`
```c
cb_status_t cb_physics_fire_growth_class_alpha(cb_fire_growth_class_t growth_class,
    double *out_alpha_kw_per_s2);
```
Python: `citybrain.physics.fire_growth_class_alpha(growth_class: FireGrowthClass) -> float`.
Published t-squared growth coefficients: NFPA 72 Annex B; Karlsson &
Quintiere, *Enclosure Fire Dynamics*, 2000, table 1.1. A convenience lookup
only; `fire_heat_release_rate` takes `alpha` as an ordinary parameter.

Errors: `CB_ERR_NULL`; `CB_ERR_DOMAIN` for a value outside
`cb_fire_growth_class_t`.

Example: `fire_growth_class_alpha(FireGrowthClass.MEDIUM)` → `0.0117`.

### `cb_physics_fire_heat_release_rate`
```c
cb_status_t cb_physics_fire_heat_release_rate(double alpha_kw_per_s2, double time_s,
    double *out_hrr_kw);
```
Python: `citybrain.physics.fire_heat_release_rate(alpha_kw_per_s2, time_s) -> float`.
t-squared heat release rate Q(t) = alpha·t² — free growth phase only, not
flashover, plateau, or decay.

Errors: `CB_ERR_NULL`; `CB_ERR_NOT_FINITE`; `CB_ERR_DOMAIN` for
`alpha_kw_per_s2 <= 0` or `time_s < 0`.

Example: `fire_heat_release_rate(0.0117, 120.0)` → `168.48`.

### `cb_physics_plume_concentration`
```c
cb_status_t cb_physics_plume_concentration(double emission_rate, double wind_speed_m_s,
    double sigma_y_m, double sigma_z_m, double crosswind_offset_m,
    double receptor_height_m, double effective_source_height_m,
    double *out_concentration);
```
Python: `citybrain.physics.plume_concentration(emission_rate, wind_speed_m_s, sigma_y_m, sigma_z_m, crosswind_offset_m, receptor_height_m, effective_source_height_m) -> float`.
Ground-reflected Gaussian plume model (Turner, *Workbook of Atmospheric
Dispersion Estimates*, U.S. EPA, 1970). `sigma_y_m`/`sigma_z_m` are taken as
direct parameters — deriving them from distance and a Pasquill-Gifford
stability class is the caller's responsibility, not implemented here.

Errors: `CB_ERR_NULL`; `CB_ERR_NOT_FINITE`; `CB_ERR_DOMAIN` for
`emission_rate < 0`, `wind_speed_m_s <= 0` (calm wind is undefined in this
model — a documented limitation, not a trust check), `sigma_y_m <= 0`, or
`sigma_z_m <= 0`.

Example: `plume_concentration(10.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0)` →
`0.0015915494309189535`.

### `cb_physics_traffic_greenshields_speed`
```c
cb_status_t cb_physics_traffic_greenshields_speed(double free_flow_speed,
    double jam_density, double density, double *out_speed);
```
Python: `citybrain.physics.traffic_greenshields_speed(free_flow_speed, jam_density, density) -> float`.
Greenshields' linear speed-density relation (B. D. Greenshields, "A Study
of Traffic Capacity," *Highway Research Board Proceedings*, vol. 14, 1935,
pp. 448-477). Only physically meaningful for `0 <= density <= jam_density`;
outside that range the formula is evaluated, not rejected (see the header
comment and the "beyond jam density" test case).

Errors: `CB_ERR_NULL`; `CB_ERR_NOT_FINITE`; `CB_ERR_DOMAIN` for
`free_flow_speed <= 0`, `jam_density <= 0`, or `density < 0`.

Example: `traffic_greenshields_speed(30.0, 120.0, 60.0)` → `15.0`.

### `cb_physics_traffic_greenshields_flow`
```c
cb_status_t cb_physics_traffic_greenshields_flow(double free_flow_speed,
    double jam_density, double density, double *out_flow);
```
Python: `citybrain.physics.traffic_greenshields_flow(free_flow_speed, jam_density, density) -> float`.
The fundamental-diagram flow implied by the same relation: q(k) = k·v(k).
Same domain and errors as `cb_physics_traffic_greenshields_speed`.

Example: `traffic_greenshields_flow(30.0, 120.0, 60.0)` → `900.0`.
