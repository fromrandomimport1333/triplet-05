"""
cffi out-of-line, API-mode build script for the citybrain C core.

Builds the C core as a static library (via CMake) if it isn't built yet,
then compiles the cffi extension against it -- so `pip install -e
bindings/python` works with no separate manual build step.

The cdef below is a hand-maintained, preprocessor-free restatement of the
public C headers; keep it in sync with include/citybrain/*.h.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cffi import FFI

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent  # brain/src/
INCLUDE_DIR = REPO_ROOT / "include"
VENDOR_BUILD_DIR = Path(
    os.environ.get("CITYBRAIN_VENDOR_BUILD_DIR", str(HERE / "_vendor_build"))
)


def _find_static_lib(build_dir: Path) -> Path:
    candidates = list(build_dir.rglob("libcitybrain.a"))
    if not candidates:
        raise RuntimeError(
            f"C core build completed but libcitybrain.a was not found under "
            f"{build_dir}. Something about the CMake layout changed; see "
            f"docs/build.md."
        )
    return candidates[0]


def _ensure_c_library_built() -> Path:
    """Build citybrain_static via CMake if needed, return the path to
    libcitybrain.a. No-op if it's already built."""
    existing = list(VENDOR_BUILD_DIR.rglob("libcitybrain.a")) if VENDOR_BUILD_DIR.exists() else []
    if existing:
        return existing[0]

    cmake = shutil.which("cmake")
    if cmake is None:
        raise RuntimeError(
            "cmake was not found on PATH. The citybrain Python package builds "
            "the C core automatically as part of `pip install`, which "
            "requires cmake and a C99 compiler to be installed. See "
            "docs/build.md for the exact toolchain this project targets."
        )

    VENDOR_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    configure_cmd = [
        cmake,
        "-S", str(REPO_ROOT),
        "-B", str(VENDOR_BUILD_DIR),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCITYBRAIN_BUILD_TESTS=OFF",
        "-DCITYBRAIN_BUILD_BENCH=OFF",
        "-DCITYBRAIN_WERROR=OFF",
    ]
    build_cmd = [
        cmake, "--build", str(VENDOR_BUILD_DIR),
        "--target", "citybrain_static",
        "--parallel",
    ]
    print("citybrain: building the C core (one-time, cached under "
          f"{VENDOR_BUILD_DIR})", file=sys.stderr)
    try:
        subprocess.run(configure_cmd, check=True)
    except subprocess.CalledProcessError:
        # A stale CMakeCache.txt from a different checkout path can make
        # CMake refuse to reconfigure in place; clear it and retry once.
        print("citybrain: stale CMake cache under "
              f"{VENDOR_BUILD_DIR}; clearing it and retrying once",
              file=sys.stderr)
        shutil.rmtree(VENDOR_BUILD_DIR, ignore_errors=True)
        VENDOR_BUILD_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(configure_cmd, check=True)
    subprocess.run(build_cmd, check=True)
    return _find_static_lib(VENDOR_BUILD_DIR)


_STATIC_LIB = _ensure_c_library_built()

ffibuilder = FFI()

ffibuilder.cdef(r"""
    typedef enum {
        CB_OK = 0,
        CB_ERR_NULL = 1,
        CB_ERR_DOMAIN = 2,
        CB_ERR_NOT_FINITE = 3,
        CB_ERR_CAPACITY = 4,
        CB_ERR_STATE = 5,
        CB_ERR_NOT_FOUND = 6,
        CB_ERR_NO_SOLUTION = 7,
        CB_ERR_NO_CONVERGE = 8,
        CB_ERR_UNSUPPORTED = 9
    } cb_status_t;

    cb_status_t cb_status_message(cb_status_t status, const char **out_message);
    cb_status_t cb_version_string(const char **out_text);
    cb_status_t cb_version_numbers(int32_t *out_major, int32_t *out_minor, int32_t *out_patch);

    /* ---- fusion.h ---- */
    typedef struct {
        double likelihood_ratio;
        double weight;
        int32_t group_id;
    } cb_evidence_t;

    typedef struct {
        double lr_cap;
        double prior;
        double posterior_floor;
        double posterior_ceiling;
        int32_t group_cap_enabled;
    } cb_fusion_config_t;

    typedef struct {
        double posterior;
        double prior_log_odds;
        double evidence_log_odds;
        double posterior_log_odds;
        int32_t n_items;
        int32_t n_groups_used;
        int32_t n_items_clamped;
        int32_t n_groups_clamped;
    } cb_fusion_result_t;

    cb_status_t cb_fusion_config_init(cb_fusion_config_t *cfg);
    cb_status_t cb_fusion_posterior(const cb_fusion_config_t *cfg,
                                     const cb_evidence_t *items, size_t n_items,
                                     double *group_scratch, size_t n_groups,
                                     cb_fusion_result_t *out);
    cb_status_t cb_fusion_posterior_independent(const cb_fusion_config_t *cfg,
                                                 const cb_evidence_t *items, size_t n_items,
                                                 cb_fusion_result_t *out);
    cb_status_t cb_logit(double probability, double *out_log_odds);
    cb_status_t cb_logistic(double log_odds, double *out_probability);

    typedef struct {
        double lr_cap;
        double prior;
        double threshold;
        double max_weight;
        double odds_multiplier;
    } cb_ft_params_t;

    cb_status_t cb_ft_params_init(cb_ft_params_t *params);
    cb_status_t cb_ft_min_sources(const cb_ft_params_t *params, int32_t *out_min_sources);
    cb_status_t cb_ft_max_posterior(const cb_ft_params_t *params, int32_t n_sources,
                                     double *out_posterior);
    cb_status_t cb_ft_check_invariant(const cb_ft_params_t *params, int32_t n_controlled,
                                       int32_t *out_safe);

    /* ---- detector.h ---- */
    typedef struct {
        double ewma_alpha;
        double var_alpha;
        double min_sigma;
        double cusum_slack;
        double cusum_threshold;
        int32_t baseline_hold_enabled;
        double baseline_hold_frac;
        uint32_t warmup_samples;
    } cb_detector_config_t;

    typedef struct {
        double ewma;
        double ewma_var;
        double cusum_high;
        double cusum_low;
        uint64_t n_seen;
        uint32_t magic;
    } cb_detector_state_t;

    typedef struct {
        uint32_t flags;
        double baseline;
        double sigma;
        double residual;
        double normalised_residual;
        double cusum_high;
        double cusum_low;
    } cb_detector_output_t;

    cb_status_t cb_detector_config_init(cb_detector_config_t *cfg);
    cb_status_t cb_detector_config_validate(const cb_detector_config_t *cfg);
    cb_status_t cb_detector_init(cb_detector_state_t *state, const cb_detector_config_t *cfg,
                                  double first_value, double initial_sigma);
    cb_status_t cb_detector_update(cb_detector_state_t *state, const cb_detector_config_t *cfg,
                                    double value, cb_detector_output_t *out);
    cb_status_t cb_detector_reset_alarms(cb_detector_state_t *state);
    cb_status_t cb_detector_baseline(const cb_detector_state_t *state,
                                      const cb_detector_config_t *cfg,
                                      double *out_baseline, double *out_sigma);
    cb_status_t cb_detector_arl0(double cusum_slack, double cusum_threshold, double *out_arl0);
    cb_status_t cb_detector_arl1(double cusum_slack, double cusum_threshold,
                                  double shift_sigmas, double *out_arl1);

    /* ---- graph.h ---- */
    typedef enum { CB_EDGE_NONE = 0, CB_EDGE_RAISES = 1, CB_EDGE_LOWERS = 2 } cb_edge_kind_t;

    typedef struct {
        double m_min;
        double m_max;
        int32_t n_nodes;
    } cb_graph_config_t;

    typedef struct {
        cb_graph_config_t cfg;
        double multiplier[8][8];
        uint8_t kind[8][8];
        uint32_t magic;
    } cb_graph_t;

    cb_status_t cb_graph_config_init(cb_graph_config_t *cfg);
    cb_status_t cb_graph_init(cb_graph_t *graph, const cb_graph_config_t *cfg);
    cb_status_t cb_graph_set_edge(cb_graph_t *graph, int32_t from, int32_t to,
                                   cb_edge_kind_t kind, double multiplier);
    cb_status_t cb_graph_get_edge(const cb_graph_t *graph, int32_t from, int32_t to,
                                   cb_edge_kind_t *out_kind, double *out_multiplier);
    cb_status_t cb_graph_clear_edge(cb_graph_t *graph, int32_t from, int32_t to);
    cb_status_t cb_graph_path_multiplier(const cb_graph_t *graph, const int32_t *path,
                                          size_t path_len, double *out_multiplier);
    cb_status_t cb_graph_extremal_multiplier(const cb_graph_t *graph, int32_t from, int32_t to,
                                              double *out_min, double *out_max);
    cb_status_t cb_graph_adjust_prior(const cb_graph_t *graph, int32_t target,
                                       const int32_t *active, size_t n_active, double prior,
                                       double *out_adjusted_prior, double *out_multiplier);
    cb_status_t cb_graph_has_unrelated_pair(const cb_graph_t *graph, const int32_t *active,
                                             size_t n_active, int32_t *out_unrelated);

    typedef enum {
        CB_EXAMPLE_FLOOD = 0, CB_EXAMPLE_FIRE = 1, CB_EXAMPLE_ACCIDENT = 2, CB_EXAMPLE_JAM = 3
    } cb_example_category_t;
    cb_status_t cb_example_load_graph(cb_graph_t *graph);

    /* ---- decision.h ---- */
    typedef enum { CB_ACTION_HOLD = 0, CB_ACTION_ACT = 1 } cb_action_t;

    typedef struct {
        double cost_act_when_false;
        double cost_hold_when_true;
        double cost_act_when_true;
        double cost_hold_when_false;
    } cb_cost_table_t;

    typedef struct {
        int32_t action;
        double expected_cost_act;
        double expected_cost_hold;
        double indifference_threshold;
        double margin;
    } cb_decision_result_t;

    cb_status_t cb_cost_table_init(cb_cost_table_t *table);
    cb_status_t cb_decision_expected_cost(const cb_cost_table_t *table, double posterior,
                                           cb_decision_result_t *out);

    typedef struct {
        int32_t action_id;
        double cost_if_true;
        double cost_if_false;
    } cb_action_cost_t;

    cb_status_t cb_decision_ladder(const cb_action_cost_t *actions, size_t n_actions,
                                    double posterior, size_t *out_index,
                                    double *out_expected_cost);

    /* ---- assignment.h ---- */
    typedef struct {
        const double *matrix;
        int32_t n_rows;
        int32_t n_cols;
        int32_t maximize;
        double infeasible_at;
    } cb_assign_params_t;

    cb_status_t cb_assign_params_init(cb_assign_params_t *params);
    cb_status_t cb_assign_scratch_bytes(int32_t n_rows, int32_t n_cols, size_t *out_bytes);
    cb_status_t cb_assign_solve(const cb_assign_params_t *params, int32_t *out_assignment,
                                 double *out_total, void *scratch, size_t scratch_bytes);

    /* ---- cache.h ---- */
    typedef struct {
        int32_t category_id;
        int32_t partition_id;
        int32_t bucket_id;
        uint64_t state_version;
        uint64_t hash;
    } cb_cache_key_t;

    cb_status_t cb_cache_bucket(const double *edges, size_t n_edges, double value,
                                 int32_t *out_bucket);
    cb_status_t cb_cache_key_make(int32_t category_id, int32_t partition_id, int32_t bucket_id,
                                   uint64_t state_version, cb_cache_key_t *out_key);
    cb_status_t cb_cache_key_equal(const cb_cache_key_t *a, const cb_cache_key_t *b,
                                    int32_t *out_equal);
    cb_status_t cb_cache_key_valid(const cb_cache_key_t *key, uint64_t current_state_version,
                                    uint64_t created_ms, uint64_t now_ms, uint64_t ttl_ms,
                                    int32_t *out_valid, uint32_t *out_reason);

    /* ---- calibration.h ---- */
    typedef struct {
        double lo;
        double hi;
        double max_step;
        double rate;
    } cb_calibration_bounds_t;

    typedef struct {
        double value;
        double raw_step;
        double applied_step;
        int32_t step_clipped;
        int32_t bound_clipped;
    } cb_calibration_result_t;

    cb_status_t cb_calibration_bounds_init(cb_calibration_bounds_t *bounds);
    cb_status_t cb_calibration_step(const cb_calibration_bounds_t *bounds, double current,
                                     double gradient, cb_calibration_result_t *out);
    cb_status_t cb_calibration_min_steps(const cb_calibration_bounds_t *bounds, double from,
                                          double to, int32_t *out_steps);

    /* ---- mdp.h ---- */
    typedef struct {
        const double *transition;
        const double *reward;
        int32_t n_states;
        int32_t n_actions;
        double discount;
        double tolerance;
        int32_t max_iterations;
        double row_sum_tolerance;
    } cb_mdp_problem_t;

    cb_status_t cb_mdp_problem_init(cb_mdp_problem_t *problem);
    cb_status_t cb_mdp_scratch_doubles(int32_t n_states, size_t *out_doubles);
    cb_status_t cb_mdp_solve(const cb_mdp_problem_t *problem, int32_t *out_policy,
                              double *out_value, double *scratch, size_t scratch_doubles,
                              int32_t *out_iterations);
    cb_status_t cb_mdp_state_index(const int32_t *dims, const int32_t *coords, size_t n_dims,
                                    int32_t *out_index);
    cb_status_t cb_mdp_state_decode(const int32_t *dims, size_t n_dims, int32_t index,
                                     int32_t *out_coords);
    cb_status_t cb_mdp_state_count(const int32_t *dims, size_t n_dims, int32_t *out_count);

    /* ---- physics.h ---- */
    cb_status_t cb_physics_gaussian_likelihood_ratio(double observed,
                                                       double expected_h1,
                                                       double expected_h0,
                                                       double sigma,
                                                       double *out_likelihood_ratio);

    cb_status_t cb_physics_flood_rise_rate(double area_m2,
                                            double inflow_m3_per_s,
                                            double drainage_coefficient,
                                            double drainage_exponent,
                                            double water_level_m,
                                            double *out_rate_m_per_s);

    typedef enum {
        CB_FIRE_GROWTH_SLOW = 0, CB_FIRE_GROWTH_MEDIUM = 1,
        CB_FIRE_GROWTH_FAST = 2, CB_FIRE_GROWTH_ULTRAFAST = 3
    } cb_fire_growth_class_t;

    cb_status_t cb_physics_fire_growth_class_alpha(cb_fire_growth_class_t growth_class,
                                                    double *out_alpha_kw_per_s2);

    cb_status_t cb_physics_fire_heat_release_rate(double alpha_kw_per_s2,
                                                   double time_s,
                                                   double *out_hrr_kw);

    cb_status_t cb_physics_plume_concentration(double emission_rate,
                                                double wind_speed_m_s,
                                                double sigma_y_m,
                                                double sigma_z_m,
                                                double crosswind_offset_m,
                                                double receptor_height_m,
                                                double effective_source_height_m,
                                                double *out_concentration);

    cb_status_t cb_physics_traffic_greenshields_speed(double free_flow_speed,
                                                       double jam_density,
                                                       double density,
                                                       double *out_speed);

    cb_status_t cb_physics_traffic_greenshields_flow(double free_flow_speed,
                                                      double jam_density,
                                                      double density,
                                                      double *out_flow);
""")

ffibuilder.set_source(
    "citybrain._citybrain_cffi",
    '#include "citybrain/citybrain.h"',
    include_dirs=[str(INCLUDE_DIR)],
    extra_objects=[str(_STATIC_LIB)],
    libraries=["m"] if sys.platform != "win32" else [],
)

if __name__ == "__main__":
    ffibuilder.compile(verbose=True)
