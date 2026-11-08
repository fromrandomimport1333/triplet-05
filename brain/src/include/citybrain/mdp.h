/*
 * Tabular MDP solver (value iteration) over a dense, caller-supplied
 * model, plus index helpers for a product state space. Precomputes one
 * best action per state so the run-time cost is a table lookup.
 *
 * Tractability note: the transition table is n_states * n_actions *
 * n_states doubles. A few hundred states with a handful of actions is a
 * few hundred KB and converges in milliseconds; this is a dense
 * representation and stops being the right one well before n_states hits
 * the hard cap below.
 */
#ifndef CITYBRAIN_MDP_H
#define CITYBRAIN_MDP_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_MDP_MAX_STATES  ((int32_t)4096)
#define CB_MDP_MAX_ACTIONS ((int32_t)64)
#define CB_MDP_MAX_DIMS    ((size_t)16)

typedef struct cb_mdp_problem {
    const double *transition; /* [s*n_actions*n_states + a*n_states + s2] */
    const double *reward;     /* [s*n_actions + a] */
    int32_t n_states;
    int32_t n_actions;
    double  discount;          /* in [0, 1) */
    double  tolerance;         /* > 0, convergence tolerance */
    int32_t max_iterations;    /* > 0 */
    double  row_sum_tolerance; /* >= 0, allowed deviation from a transition row summing to 1 */
} cb_mdp_problem_t;

/* Default: discount=0.95, tolerance=1e-10, max_iterations=10000,
 * row_sum_tolerance=1e-9. */
CB_API cb_status_t cb_mdp_problem_init(cb_mdp_problem_t *problem);

/* Doubles of scratch required: 2 * n_states. */
CB_API cb_status_t cb_mdp_scratch_doubles(int32_t n_states, size_t *out_doubles);

/*
 * Value iteration. Ties in the action maximisation go to the lowest
 * action index. Errors: CB_ERR_DOMAIN for a transition row that doesn't
 * sum to 1 (within tolerance) or a negative probability; CB_ERR_CAPACITY
 * for dimensions above the limits above; CB_ERR_NO_CONVERGE if the
 * iteration limit is hit first (policy/value from the last iteration are
 * still written).
 */
CB_API cb_status_t cb_mdp_solve(const cb_mdp_problem_t *problem,
                                int32_t *out_policy,
                                double *out_value,
                                double *scratch,
                                size_t scratch_doubles,
                                int32_t *out_iterations);

/* Mixed-radix index helpers for a product state space (row-major, last
 * dimension fastest), so callers agree on one layout. */
CB_API cb_status_t cb_mdp_state_index(const int32_t *dims,
                                      const int32_t *coords,
                                      size_t n_dims,
                                      int32_t *out_index);

CB_API cb_status_t cb_mdp_state_decode(const int32_t *dims,
                                       size_t n_dims,
                                       int32_t index,
                                       int32_t *out_coords);

CB_API cb_status_t cb_mdp_state_count(const int32_t *dims,
                                      size_t n_dims,
                                      int32_t *out_count);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_MDP_H */
