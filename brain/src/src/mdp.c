#include "citybrain/mdp.h"
#include "internal.h"

cb_status_t cb_mdp_problem_init(cb_mdp_problem_t *problem)
{
    CB_REQUIRE(problem != NULL, CB_ERR_NULL);
    problem->transition        = NULL;
    problem->reward            = NULL;
    problem->n_states          = 0;
    problem->n_actions         = 0;
    problem->discount          = 0.95;
    problem->tolerance         = 1e-10;
    problem->max_iterations    = 10000;
    problem->row_sum_tolerance = 1e-9;
    return CB_OK;
}

cb_status_t cb_mdp_scratch_doubles(int32_t n_states, size_t *out_doubles)
{
    CB_REQUIRE(out_doubles != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_states >= 1, CB_ERR_DOMAIN);
    CB_REQUIRE(n_states <= CB_MDP_MAX_STATES, CB_ERR_CAPACITY);
    *out_doubles = (size_t)n_states * 2u;
    return CB_OK;
}

cb_status_t cb_mdp_solve(const cb_mdp_problem_t *problem,
                         int32_t *out_policy, double *out_value,
                         double *scratch, size_t scratch_doubles,
                         int32_t *out_iterations)
{
    size_t n, a_count, s, a, s2;
    double *value, *next;
    int32_t iteration = 0;
    int converged = 0;

    CB_REQUIRE(problem != NULL, CB_ERR_NULL);
    CB_REQUIRE(problem->transition != NULL && problem->reward != NULL, CB_ERR_NULL);
    CB_REQUIRE(scratch != NULL, CB_ERR_NULL);
    CB_REQUIRE(problem->n_states >= 1 && problem->n_actions >= 1, CB_ERR_DOMAIN);
    CB_REQUIRE(problem->n_states <= CB_MDP_MAX_STATES, CB_ERR_CAPACITY);
    CB_REQUIRE(problem->n_actions <= CB_MDP_MAX_ACTIONS, CB_ERR_CAPACITY);
    CB_REQUIRE(cb_finite(problem->discount) && cb_finite(problem->tolerance) &&
               cb_finite(problem->row_sum_tolerance), CB_ERR_NOT_FINITE);
    CB_REQUIRE(problem->discount >= 0.0 && problem->discount < 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(problem->tolerance > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(problem->row_sum_tolerance >= 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(problem->max_iterations >= 1, CB_ERR_DOMAIN);

    n = (size_t)problem->n_states;
    a_count = (size_t)problem->n_actions;
    CB_REQUIRE(scratch_doubles >= 2u * n, CB_ERR_CAPACITY);

    CB_CHECK(cb_finite_array(problem->reward, n * a_count));
    CB_CHECK(cb_finite_array(problem->transition, n * a_count * n));

    /* A row that doesn't sum to one is a modelling error, not a silent wrong answer. */
    for (s = 0; s < n; ++s) {
        for (a = 0; a < a_count; ++a) {
            const double *row = problem->transition + (s * a_count + a) * n;
            double sum = 0.0;
            for (s2 = 0; s2 < n; ++s2) {
                CB_REQUIRE(row[s2] >= 0.0, CB_ERR_DOMAIN);
                sum += row[s2];
            }
            CB_REQUIRE(fabs(sum - 1.0) <= problem->row_sum_tolerance, CB_ERR_DOMAIN);
        }
    }

    value = scratch;
    next  = scratch + n;
    for (s = 0; s < n; ++s) {
        value[s] = 0.0;
        next[s] = 0.0;
    }

    for (iteration = 1; iteration <= problem->max_iterations; ++iteration) {
        double delta = 0.0;
        for (s = 0; s < n; ++s) {
            double best = 0.0;
            for (a = 0; a < a_count; ++a) {
                const double *row = problem->transition + (s * a_count + a) * n;
                double q = problem->reward[s * a_count + a];
                double acc = 0.0;
                for (s2 = 0; s2 < n; ++s2) {
                    acc += row[s2] * value[s2];
                }
                q += problem->discount * acc;
                /* Strict > keeps the lowest action index on a tie. */
                if (a == 0 || q > best) {
                    best = q;
                }
            }
            next[s] = best;
            {
                double d = fabs(next[s] - value[s]);
                if (d > delta) { delta = d; }
            }
        }
        for (s = 0; s < n; ++s) {
            value[s] = next[s];
        }
        if (delta <= problem->tolerance) {
            converged = 1;
            break;
        }
    }
    if (iteration > problem->max_iterations) {
        iteration = problem->max_iterations;
    }

    /* One greedy pass to turn the converged value function into a policy. */
    if (out_policy != NULL) {
        for (s = 0; s < n; ++s) {
            double best = 0.0;
            int32_t best_a = 0;
            for (a = 0; a < a_count; ++a) {
                const double *row = problem->transition + (s * a_count + a) * n;
                double q = problem->reward[s * a_count + a];
                double acc = 0.0;
                for (s2 = 0; s2 < n; ++s2) {
                    acc += row[s2] * value[s2];
                }
                q += problem->discount * acc;
                if (a == 0 || q > best) {
                    best = q;
                    best_a = (int32_t)a;
                }
            }
            out_policy[s] = best_a;
        }
    }
    if (out_value != NULL) {
        for (s = 0; s < n; ++s) {
            out_value[s] = value[s];
        }
    }
    if (out_iterations != NULL) {
        *out_iterations = iteration;
    }
    return converged ? CB_OK : CB_ERR_NO_CONVERGE;
}

static cb_status_t dims_validate(const int32_t *dims, size_t n_dims,
                                 int64_t *out_total)
{
    size_t i;
    int64_t total = 1;

    CB_REQUIRE(dims != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_dims > 0 && n_dims <= CB_MDP_MAX_DIMS, CB_ERR_DOMAIN);
    for (i = 0; i < n_dims; ++i) {
        CB_REQUIRE(dims[i] >= 1, CB_ERR_DOMAIN);
        total *= (int64_t)dims[i];
        CB_REQUIRE(total <= 2147483647LL, CB_ERR_CAPACITY);
    }
    if (out_total != NULL) { *out_total = total; }
    return CB_OK;
}

cb_status_t cb_mdp_state_count(const int32_t *dims, size_t n_dims, int32_t *out_count)
{
    int64_t total = 1;

    CB_REQUIRE(out_count != NULL, CB_ERR_NULL);
    CB_CHECK(dims_validate(dims, n_dims, &total));
    *out_count = (int32_t)total;
    return CB_OK;
}

cb_status_t cb_mdp_state_index(const int32_t *dims, const int32_t *coords,
                               size_t n_dims, int32_t *out_index)
{
    int64_t index = 0;
    size_t i;

    CB_REQUIRE(out_index != NULL && coords != NULL, CB_ERR_NULL);
    CB_CHECK(dims_validate(dims, n_dims, NULL));

    /* Row-major, last dimension fastest. */
    for (i = 0; i < n_dims; ++i) {
        CB_REQUIRE(coords[i] >= 0 && coords[i] < dims[i], CB_ERR_DOMAIN);
        index = index * (int64_t)dims[i] + (int64_t)coords[i];
        CB_REQUIRE(index <= 2147483647LL, CB_ERR_CAPACITY);
    }
    *out_index = (int32_t)index;
    return CB_OK;
}

cb_status_t cb_mdp_state_decode(const int32_t *dims, size_t n_dims,
                                int32_t index, int32_t *out_coords)
{
    int64_t total = 1;
    size_t i;

    CB_REQUIRE(out_coords != NULL, CB_ERR_NULL);
    CB_CHECK(dims_validate(dims, n_dims, &total));
    CB_REQUIRE(index >= 0 && (int64_t)index < total, CB_ERR_DOMAIN);

    for (i = n_dims; i > 0; --i) {
        out_coords[i - 1] = index % dims[i - 1];
        index /= dims[i - 1];
    }
    return CB_OK;
}
