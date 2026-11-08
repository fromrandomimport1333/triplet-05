/* Randomised property tests, sampling the documented parameter space rather
 * than single worked examples. Case count is settable: ./test_property
 * [cases] (default 10000). The seed is fixed and printed for reproducibility. */
#include "cb_test.h"

typedef struct rng { uint64_t s; } rng_t;

static uint64_t rng_next(rng_t *r)
{
    /* splitmix64: short, well-distributed, and reproducible across platforms. */
    uint64_t z;
    r->s += 0x9E3779B97F4A7C15ULL;
    z = r->s;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

static double rng_unit(rng_t *r)
{
    return (double)(rng_next(r) >> 11) / 9007199254740992.0;
}

static double rng_range(rng_t *r, double lo, double hi)
{
    return lo + (hi - lo) * rng_unit(r);
}

/* Log-uniform, so likelihood ratios span many orders of magnitude rather than
 * clustering near the top of a linear range. */
static double rng_log_range(rng_t *r, double lo, double hi)
{
    return exp(rng_range(r, log(lo), log(hi)));
}

static int32_t rng_int(rng_t *r, int32_t lo, int32_t hi)
{
    return lo + (int32_t)(rng_next(r) % (uint64_t)(hi - lo + 1));
}

/* ------------------------------------------------------------------ */

static long prop_bound_is_tight(rng_t *r, long cases)
{
    long ran = 0;
    long i;
    static cb_evidence_t items[256];

    CB_CASE("property: n_min groups cross the threshold and n_min-1 do not");
    for (i = 0; i < cases; ++i) {
        cb_ft_params_t ft;
        cb_fusion_config_t cfg;
        cb_fusion_result_t res;
        int32_t n_min, k;

        CB_CHECK_OK(cb_ft_params_init(&ft));
        ft.lr_cap     = rng_log_range(r, 1.05, 1000.0);
        ft.prior      = rng_log_range(r, 1e-6, 0.4);
        ft.threshold  = rng_range(r, 0.01, 0.99);
        ft.max_weight = rng_range(r, 0.05, 1.0);
        ft.odds_multiplier = rng_log_range(r, 0.1, 10.0);

        if (cb_ft_min_sources(&ft, &n_min) != CB_OK) { continue; }
        if (n_min < 1 || n_min > 200) { continue; }

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        cfg.lr_cap = ft.lr_cap;
        /* Fold the correlation-graph multiplier into the prior exactly as the
         * pipeline does, so the test exercises the same composition. */
        {
            double l0, adjusted;
            CB_CHECK_OK(cb_logit(ft.prior, &l0));
            l0 += log(ft.odds_multiplier);
            CB_CHECK_OK(cb_logistic(l0, &adjusted));
            if (!(adjusted > 0.0 && adjusted < 1.0)) { continue; }
            cfg.prior = adjusted;
        }

        for (k = 0; k < 256; ++k) {
            items[k].likelihood_ratio = 1e300; /* always clipped to the cap */
            items[k].weight = ft.max_weight;
            items[k].group_id = k;
        }

        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items,
                                                    (size_t)(n_min - 1), &res));
        CB_CHECK_TRUE(res.posterior < ft.threshold);
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items,
                                                    (size_t)n_min, &res));
        CB_CHECK_TRUE(res.posterior >= ft.threshold);
        ran++;
    }
    printf("  bound tightness: %ld of %ld sampled configurations in range\n",
           ran, cases);
    return ran;
}

static long prop_no_evidence_exceeds_the_bound(rng_t *r, long cases)
{
    long ran = 0;
    long i;
    static cb_evidence_t items[64];
    static double scratch[16];

    CB_CASE("property: real fused evidence never exceeds the derived ceiling");
    for (i = 0; i < cases; ++i) {
        cb_fusion_config_t cfg;
        cb_fusion_result_t res;
        cb_ft_params_t ft;
        double ceiling;
        size_t n_items;
        size_t n_groups;
        size_t k;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        cfg.lr_cap = rng_log_range(r, 1.05, 500.0);
        cfg.prior  = rng_log_range(r, 1e-6, 0.5);
        n_groups = (size_t)rng_int(r, 1, 16);
        n_items  = (size_t)rng_int(r, 0, 64);

        for (k = 0; k < n_items; ++k) {
            items[k].likelihood_ratio = rng_log_range(r, 1e-12, 1e12);
            items[k].weight = rng_unit(r);
            items[k].group_id = rng_int(r, 0, (int32_t)n_groups - 1);
        }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, n_items, scratch, n_groups, &res));

        CB_CHECK_OK(cb_ft_params_init(&ft));
        ft.lr_cap = cfg.lr_cap;
        ft.prior = cfg.prior;
        ft.max_weight = 1.0;
        CB_CHECK_OK(cb_ft_max_posterior(&ft, res.n_groups_used, &ceiling));

        /* The posterior is capped by the number of independent groups, whatever shape the evidence takes. */
        CB_CHECK_TRUE(res.posterior <= ceiling + 1e-12);
        CB_CHECK_TRUE(res.n_groups_used <= (int32_t)n_groups);
        CB_CHECK_TRUE(res.posterior >= 0.0 && res.posterior <= 1.0);
        CB_CHECK_TRUE(!isnan(res.posterior));
        ran++;
    }
    printf("  evidence ceiling: %ld cases\n", ran);
    return ran;
}

static long prop_grouping_never_helps_the_attacker(rng_t *r, long cases)
{
    long ran = 0;
    long i;
    static cb_evidence_t items[32];
    static double scratch[32];

    CB_CASE("property: merging items into a group never raises the posterior");
    for (i = 0; i < cases; ++i) {
        cb_fusion_config_t cfg;
        cb_fusion_result_t grouped, separate;
        size_t n_items;
        size_t k;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        cfg.lr_cap = rng_log_range(r, 1.05, 100.0);
        cfg.prior  = rng_log_range(r, 1e-5, 0.4);
        n_items = (size_t)rng_int(r, 1, 32);

        /* All items positive evidence, all in one group versus all separate. */
        for (k = 0; k < n_items; ++k) {
            items[k].likelihood_ratio = rng_log_range(r, 1.0, 1e6);
            items[k].weight = rng_unit(r);
            items[k].group_id = 0;
        }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, n_items, scratch, 32, &grouped));
        for (k = 0; k < n_items; ++k) { items[k].group_id = (int32_t)k; }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, n_items, scratch, 32, &separate));

        CB_CHECK_TRUE(grouped.posterior <= separate.posterior + 1e-15);
        ran++;
    }
    printf("  grouping monotonicity: %ld cases\n", ran);
    return ran;
}

static long prop_monotone_in_evidence(rng_t *r, long cases)
{
    long ran = 0;
    long i;

    CB_CASE("property: raising a likelihood ratio never lowers the posterior");
    for (i = 0; i < cases; ++i) {
        cb_fusion_config_t cfg;
        cb_fusion_result_t a, b;
        cb_evidence_t items[4];
        size_t k;
        double bump;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        cfg.lr_cap = rng_log_range(r, 1.05, 100.0);
        cfg.prior  = rng_log_range(r, 1e-5, 0.5);
        for (k = 0; k < 4; ++k) {
            items[k].likelihood_ratio = rng_log_range(r, 1e-6, 1e6);
            items[k].weight = rng_unit(r);
            items[k].group_id = (int32_t)k;
        }
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 4, &a));
        bump = rng_log_range(r, 1.0, 100.0);
        items[0].likelihood_ratio *= bump;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 4, &b));
        CB_CHECK_TRUE(b.posterior >= a.posterior - 1e-15);
        ran++;
    }
    printf("  evidence monotonicity: %ld cases\n", ran);
    return ran;
}

static long prop_graph_bounds(rng_t *r, long cases)
{
    long ran = 0;
    long i;

    CB_CASE("property: every composed multiplier stays inside [m_min, m_max]");
    for (i = 0; i < cases; ++i) {
        cb_graph_config_t cfg;
        cb_graph_t g;
        int32_t path[CB_GRAPH_MAX_NODES];
        int32_t a, b, len, k;
        double m, adjusted, mult;
        int32_t active[CB_GRAPH_MAX_NODES];

        CB_CHECK_OK(cb_graph_config_init(&cfg));
        cfg.m_min = rng_range(r, 0.01, 1.0);
        cfg.m_max = rng_range(r, 1.0, 20.0);
        cfg.n_nodes = rng_int(r, 2, CB_GRAPH_MAX_NODES);
        CB_CHECK_OK(cb_graph_init(&g, &cfg));

        for (a = 0; a < cfg.n_nodes; ++a) {
            for (b = 0; b < cfg.n_nodes; ++b) {
                if (a == b) { continue; }
                if (rng_unit(r) < 0.5) { continue; }
                if (rng_unit(r) < 0.5) {
                    CB_CHECK_OK(cb_graph_set_edge(&g, a, b, CB_EDGE_LOWERS,
                                                  rng_range(r, 1e-6, 1.0)));
                } else {
                    CB_CHECK_OK(cb_graph_set_edge(&g, a, b, CB_EDGE_RAISES,
                                                  rng_range(r, 1.0, 1e6)));
                }
            }
        }

        len = rng_int(r, 1, cfg.n_nodes);
        for (k = 0; k < len; ++k) {
            path[k] = rng_int(r, 0, cfg.n_nodes - 1);
            active[k] = path[k];
        }
        CB_CHECK_OK(cb_graph_path_multiplier(&g, path, (size_t)len, &m));
        CB_CHECK_TRUE(m >= cfg.m_min && m <= cfg.m_max);

        CB_CHECK_OK(cb_graph_adjust_prior(&g, rng_int(r, 0, cfg.n_nodes - 1),
                                          active, (size_t)len,
                                          rng_log_range(r, 1e-6, 0.5),
                                          &adjusted, &mult));
        CB_CHECK_TRUE(mult >= cfg.m_min && mult <= cfg.m_max);
        CB_CHECK_TRUE(adjusted > 0.0 && adjusted < 1.0);
        ran++;
    }
    printf("  graph bounds: %ld cases\n", ran);
    return ran;
}

static long prop_calibration_bounds(rng_t *r, long cases)
{
    long ran = 0;
    long i;

    CB_CASE("property: a bounded parameter never escapes, however hostile the gradient");
    for (i = 0; i < cases; ++i) {
        cb_calibration_bounds_t b;
        cb_calibration_result_t res;
        double theta;
        int step;
        int32_t min_steps;

        b.lo = rng_range(r, -100.0, 100.0);
        b.hi = b.lo + rng_log_range(r, 1e-3, 100.0);
        b.max_step = rng_log_range(r, 1e-6, 10.0);
        b.rate = rng_range(r, -10.0, 10.0);
        theta = rng_range(r, b.lo, b.hi);

        for (step = 0; step < 20; ++step) {
            double gradient = rng_range(r, -1e9, 1e9);
            CB_CHECK_OK(cb_calibration_step(&b, theta, gradient, &res));
            CB_CHECK_TRUE(fabs(res.applied_step) <= b.max_step + 1e-12);
            theta = res.value;
            CB_CHECK_TRUE(theta >= b.lo && theta <= b.hi);
        }
        if (cb_calibration_min_steps(&b, b.lo, b.hi, &min_steps) == CB_OK) {
            CB_CHECK_TRUE((double)min_steps * b.max_step >= (b.hi - b.lo) - 1e-9);
        }
        ran++;
    }
    printf("  calibration bounds: %ld cases\n", ran);
    return ran;
}

static long prop_decision_threshold(rng_t *r, long cases)
{
    long ran = 0;
    long i;

    CB_CASE("property: the decision agrees with its own indifference threshold");
    for (i = 0; i < cases; ++i) {
        cb_cost_table_t t;
        cb_decision_result_t res;
        double p;

        t.cost_act_when_false = rng_range(r, 0.0, 100.0);
        t.cost_hold_when_true         = rng_range(r, 0.0, 1000.0);
        t.cost_act_when_true  = rng_range(r, -10.0, 10.0);
        t.cost_hold_when_false      = rng_range(r, -10.0, 10.0);
        p = rng_unit(r);

        CB_CHECK_OK(cb_decision_expected_cost(&t, p, &res));
        if (res.indifference_threshold >= 0.0 && res.indifference_threshold <= 1.0) {
            if (p > res.indifference_threshold + 1e-9) {
                CB_CHECK_EQ_INT(res.action, CB_ACTION_ACT);
            } else if (p < res.indifference_threshold - 1e-9) {
                CB_CHECK_EQ_INT(res.action, CB_ACTION_HOLD);
            }
        }
        ran++;
    }
    printf("  decision threshold: %ld cases\n", ran);
    return ran;
}

static long prop_assignment_optimal(rng_t *r, long cases)
{
    long ran = 0;
    long i;
    static unsigned char scratch[1u << 16];

    CB_CASE("property: no random permutation beats the Hungarian total");
    for (i = 0; i < cases; ++i) {
        cb_assign_params_t p;
        double m[36];
        int32_t a[6];
        double total;
        int32_t n = rng_int(r, 1, 6);
        int32_t k, trial;

        for (k = 0; k < n * n; ++k) {
            m[k] = rng_range(r, -50.0, 50.0);
        }
        CB_CHECK_OK(cb_assign_params_init(&p));
        p.matrix = m; p.n_rows = n; p.n_cols = n;
        CB_CHECK_OK(cb_assign_solve(&p, a, &total, scratch, sizeof scratch));

        for (trial = 0; trial < 20; ++trial) {
            int32_t perm[6];
            double candidate = 0.0;
            for (k = 0; k < n; ++k) { perm[k] = k; }
            for (k = n - 1; k > 0; --k) {
                int32_t j = rng_int(r, 0, k);
                int32_t tmp = perm[k]; perm[k] = perm[j]; perm[j] = tmp;
            }
            for (k = 0; k < n; ++k) { candidate += m[k * n + perm[k]]; }
            CB_CHECK_TRUE(total <= candidate + 1e-9);
        }
        ran++;
    }
    printf("  assignment optimality: %ld cases\n", ran);
    return ran;
}

int main(int argc, char **argv)
{
    rng_t r;
    long cases = 10000;
    long total_cases = 0;
    const uint64_t seed = 0x5EED1234ABCD0001ULL;

    if (argc > 1) {
        cases = atol(argv[1]);
        if (cases < 1) { cases = 1; }
    }
    r.s = seed;

    printf("test_property: seed 0x%016llx, %ld cases per property\n",
           (unsigned long long)seed, cases);

    total_cases += prop_bound_is_tight(&r, cases);
    total_cases += prop_no_evidence_exceeds_the_bound(&r, cases);
    total_cases += prop_grouping_never_helps_the_attacker(&r, cases);
    total_cases += prop_monotone_in_evidence(&r, cases);
    total_cases += prop_graph_bounds(&r, cases);
    total_cases += prop_calibration_bounds(&r, cases);
    total_cases += prop_decision_threshold(&r, cases);
    total_cases += prop_assignment_optimal(&r, cases / 10 + 1);

    printf("test_property: %ld randomised cases executed in total\n", total_cases);
    return cb_t_report("test_property");
}
