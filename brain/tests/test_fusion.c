/* Unit and edge-case tests for log-odds fusion and the fault-tolerance
 * inequality. The adversarial scenarios live in test_adversarial.c. */
#include "cb_test.h"

static cb_evidence_t ev(double lr, double w, int32_t g)
{
    cb_evidence_t e;
    e.likelihood_ratio = lr;
    e.weight = w;
    e.group_id = g;
    return e;
}

static void test_logit_logistic(void)
{
    double x, p;

    CB_CASE("logit/logistic round trip");
    CB_CHECK_OK(cb_logit(0.5, &x));
    CB_CHECK_NEAR(x, 0.0, 0.0);
    CB_CHECK_OK(cb_logistic(0.0, &p));
    CB_CHECK_NEAR(p, 0.5, 0.0);

    CB_CHECK_OK(cb_logit(0.02, &x));
    CB_CHECK_NEAR(x, log(0.02 / 0.98), 1e-15);
    CB_CHECK_OK(cb_logistic(x, &p));
    CB_CHECK_NEAR(p, 0.02, 1e-15);

    CB_CASE("logit domain boundaries");
    CB_CHECK_STATUS(cb_logit(0.0, &x), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_logit(1.0, &x), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_logit(-0.1, &x), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_logit(1.1, &x), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_logit((double)NAN, &x), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_logit(0.5, NULL), CB_ERR_NULL);

    CB_CASE("logistic saturates without NaN");
    CB_CHECK_OK(cb_logistic(-800.0, &p));
    CB_CHECK_TRUE(p >= 0.0 && p <= 1.0 && !isnan(p));
    CB_CHECK_OK(cb_logistic(800.0, &p));
    CB_CHECK_TRUE(p >= 0.0 && p <= 1.0 && !isnan(p));
    CB_CHECK_STATUS(cb_logistic(HUGE_VAL, &p), CB_ERR_NOT_FINITE);
}

static void test_worked_example(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t items[2];

    /* Cap 8x, prior 2%: one falsified source reaches ~14%, below the ~20% threshold; two are needed. */
    CB_CASE("design brief worked example, one source");
    CB_CHECK_OK(cb_fusion_config_init(&cfg));
    CB_CHECK_NEAR(cfg.lr_cap, 8.0, 0.0);
    CB_CHECK_NEAR(cfg.prior, 0.02, 0.0);

    items[0] = ev(1000.0, 1.0, 0); /* an extreme claim, clipped to the cap */
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.posterior, 0.1403508771929825, 1e-15);
    CB_CHECK_EQ_INT(res.n_items_clamped, 1);
    CB_CHECK_TRUE(res.posterior < 0.20);

    CB_CASE("design brief worked example, two sources cross");
    items[1] = ev(1000.0, 1.0, 1);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 2, &res));
    CB_CHECK_NEAR(res.posterior, 0.5663716814159292, 1e-15);
    CB_CHECK_TRUE(res.posterior >= 0.20);

    CB_CASE("an uncapped claim would have run away");
    /* Without the cap this same evidence gives a posterior near 0.95. */
    CB_CHECK_TRUE(1000.0 * (0.02 / 0.98) / (1.0 + 1000.0 * (0.02 / 0.98)) > 0.95);
}

static void test_cap_and_weights(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t items[3];

    CB_CHECK_OK(cb_fusion_config_init(&cfg));

    CB_CASE("no evidence leaves the prior untouched");
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, NULL, 0, &res));
    CB_CHECK_NEAR(res.posterior, cfg.prior, 1e-16);
    CB_CHECK_EQ_INT(res.n_items, 0);
    CB_CHECK_EQ_INT(res.n_groups_used, 0);

    CB_CASE("likelihood ratio of exactly one changes nothing");
    items[0] = ev(1.0, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.posterior, cfg.prior, 1e-16);
    CB_CHECK_NEAR(res.evidence_log_odds, 0.0, 0.0);

    CB_CASE("weight zero silences an item entirely");
    items[0] = ev(1e9, 0.0, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.posterior, cfg.prior, 1e-16);

    CB_CASE("weight scales the capped contribution linearly");
    items[0] = ev(cfg.lr_cap, 0.5, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.evidence_log_odds, 0.5 * log(cfg.lr_cap), 1e-15);

    CB_CASE("a ratio below 1/cap is clipped at the floor");
    items[0] = ev(1e-9, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.evidence_log_odds, -log(cfg.lr_cap), 1e-15);
    CB_CHECK_EQ_INT(res.n_items_clamped, 1);

    CB_CASE("exactly at the cap is not counted as clipped");
    items[0] = ev(cfg.lr_cap, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_EQ_INT(res.n_items_clamped, 0);

    CB_CASE("contributions in opposite directions cancel");
    items[0] = ev(4.0, 1.0, 0);
    items[1] = ev(0.25, 1.0, 1);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 2, &res));
    CB_CHECK_NEAR(res.posterior, cfg.prior, 1e-15);
}

static void test_group_capping(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t items[4];
    double scratch[4];

    CB_CHECK_OK(cb_fusion_config_init(&cfg));

    CB_CASE("four items in one group count as one capped source");
    items[0] = ev(8.0, 1.0, 0);
    items[1] = ev(8.0, 1.0, 0);
    items[2] = ev(8.0, 1.0, 0);
    items[3] = ev(8.0, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 4, scratch, 4, &res));
    CB_CHECK_NEAR(res.evidence_log_odds, log(8.0), 1e-15);
    CB_CHECK_EQ_INT(res.n_groups_used, 1);
    CB_CHECK_EQ_INT(res.n_groups_clamped, 1);
    CB_CHECK_NEAR(res.posterior, 0.1403508771929825, 1e-15);

    CB_CASE("the same four items in four groups do not");
    items[1].group_id = 1;
    items[2].group_id = 2;
    items[3].group_id = 3;
    CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 4, scratch, 4, &res));
    CB_CHECK_NEAR(res.evidence_log_odds, 4.0 * log(8.0), 1e-14);
    CB_CHECK_EQ_INT(res.n_groups_used, 4);
    CB_CHECK_EQ_INT(res.n_groups_clamped, 0);

    CB_CASE("one item per group matches the independent entry point");
    {
        cb_fusion_result_t a, b;
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 4, scratch, 4, &a));
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 4, &b));
        CB_CHECK_NEAR(a.posterior, b.posterior, 0.0);
        CB_CHECK_NEAR(a.evidence_log_odds, b.evidence_log_odds, 0.0);
    }

    CB_CASE("a group summing to zero still counts as used");
    items[0] = ev(4.0, 1.0, 0);
    items[1] = ev(0.25, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 2, scratch, 4, &res));
    CB_CHECK_EQ_INT(res.n_groups_used, 1);
    CB_CHECK_NEAR(res.evidence_log_odds, 0.0, 1e-16);

    CB_CASE("group ids must be inside the scratch range");
    items[0].group_id = 9;
    CB_CHECK_STATUS(cb_fusion_posterior(&cfg, items, 2, scratch, 4, &res),
                    CB_ERR_DOMAIN);
    items[0].group_id = -1;
    CB_CHECK_STATUS(cb_fusion_posterior(&cfg, items, 2, scratch, 4, &res),
                    CB_ERR_DOMAIN);
    items[0].group_id = 0;
    CB_CHECK_STATUS(cb_fusion_posterior(&cfg, items, 2, NULL, 4, &res),
                    CB_ERR_NULL);

    CB_CASE("a rejected call leaves the scratch buffer untouched");
    {
        size_t i;
        for (i = 0; i < 4; ++i) { scratch[i] = -12345.0; }
        items[1].likelihood_ratio = -1.0;
        CB_CHECK_STATUS(cb_fusion_posterior(&cfg, items, 2, scratch, 4, &res),
                        CB_ERR_DOMAIN);
        for (i = 0; i < 4; ++i) { CB_CHECK_NEAR(scratch[i], -12345.0, 0.0); }
    }
}

static void test_fusion_domain_errors(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t items[1];

    CB_CHECK_OK(cb_fusion_config_init(&cfg));

    CB_CASE("likelihood ratio of zero is a defined error, not minus infinity");
    items[0] = ev(0.0, 1.0, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);

    CB_CASE("negative and non-finite likelihood ratios are errors");
    items[0] = ev(-2.0, 1.0, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    items[0] = ev((double)NAN, 1.0, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_NOT_FINITE);
    items[0] = ev(HUGE_VAL, 1.0, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_NOT_FINITE);

    CB_CASE("weights outside [0,1] are errors");
    items[0] = ev(2.0, -0.1, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    items[0] = ev(2.0, 1.0001, 0);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);

    CB_CASE("prior of exactly 0 or 1 is an error");
    items[0] = ev(2.0, 1.0, 0);
    cfg.prior = 0.0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    cfg.prior = 1.0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_fusion_config_init(&cfg));

    CB_CASE("a cap at or below 1 is an error");
    cfg.lr_cap = 1.0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    cfg.lr_cap = 0.5;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_fusion_config_init(&cfg));

    CB_CASE("null arguments");
    CB_CHECK_STATUS(cb_fusion_posterior_independent(NULL, items, 1, &res), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, NULL, 1, &res), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_fusion_config_init(NULL), CB_ERR_NULL);

    CB_CASE("posterior clamp");
    cfg.posterior_floor = 0.1;
    cfg.posterior_ceiling = 0.9;
    items[0] = ev(8.0, 1.0, 0);
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.posterior, 0.1403508771929825, 1e-15);
    cfg.posterior_floor = 0.2;
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    CB_CHECK_NEAR(res.posterior, 0.2, 0.0);
    cfg.posterior_floor = 0.5;
    cfg.posterior_ceiling = 0.4;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, items, 1, &res),
                    CB_ERR_DOMAIN);
}

static void test_large_n(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    static cb_evidence_t many[20000];
    size_t i;

    CB_CASE("twenty thousand items stay finite and saturate cleanly");
    CB_CHECK_OK(cb_fusion_config_init(&cfg));
    for (i = 0; i < 20000; ++i) {
        many[i] = ev(8.0, 1.0, (int32_t)i);
    }
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, many, 20000, &res));
    CB_CHECK_TRUE(!isnan(res.posterior));
    CB_CHECK_TRUE(res.posterior <= 1.0 && res.posterior >= 0.0);
    CB_CHECK_TRUE(isfinite(res.evidence_log_odds));
    CB_CHECK_NEAR(res.evidence_log_odds, 20000.0 * log(8.0), 1e-12);

    CB_CASE("item count above the documented maximum is a capacity error");
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, many,
                        CB_FUSION_MAX_ITEMS + 1, &res), CB_ERR_CAPACITY);
}

static void test_ft_inequality(void)
{
    cb_ft_params_t p;
    int32_t n, safe;
    double q;

    CB_CASE("fault-tolerance defaults reproduce the brief");
    CB_CHECK_OK(cb_ft_params_init(&p));
    CB_CHECK_OK(cb_ft_min_sources(&p, &n));
    CB_CHECK_EQ_INT(n, 2);
    CB_CHECK_OK(cb_ft_max_posterior(&p, 1, &q));
    CB_CHECK_NEAR(q, 0.1403508771929825, 1e-15);
    CB_CHECK_TRUE(q < p.threshold);
    CB_CHECK_OK(cb_ft_max_posterior(&p, 2, &q));
    CB_CHECK_TRUE(q >= p.threshold);

    CB_CASE("G3 invariant holds for one group and fails for two");
    CB_CHECK_OK(cb_ft_check_invariant(&p, 1, &safe));
    CB_CHECK_EQ_INT(safe, 1);
    CB_CHECK_OK(cb_ft_check_invariant(&p, 2, &safe));
    CB_CHECK_EQ_INT(safe, 0);

    CB_CASE("zero sources leaves the prior, which is below threshold");
    CB_CHECK_OK(cb_ft_max_posterior(&p, 0, &q));
    CB_CHECK_NEAR(q, p.prior, 1e-15);

    CB_CASE("min_sources is the smallest n that reaches the threshold");
    {
        int32_t k;
        double at_k, before_k;
        CB_CHECK_OK(cb_ft_min_sources(&p, &k));
        CB_CHECK_OK(cb_ft_max_posterior(&p, k, &at_k));
        CB_CHECK_TRUE(at_k >= p.threshold);
        if (k > 0) {
            CB_CHECK_OK(cb_ft_max_posterior(&p, k - 1, &before_k));
            CB_CHECK_TRUE(before_k < p.threshold);
        }
    }

    CB_CASE("an exact boundary resolves to the lower n");
    /* Pick T so that exactly two capped sources land on it to the bit. */
    {
        cb_ft_params_t q2;
        double l0, target, boundary;
        CB_CHECK_OK(cb_ft_params_init(&q2));
        CB_CHECK_OK(cb_logit(q2.prior, &l0));
        target = l0 + 2.0 * log(q2.lr_cap);
        CB_CHECK_OK(cb_logistic(target, &boundary));
        q2.threshold = boundary;
        CB_CHECK_OK(cb_ft_min_sources(&q2, &n));
        CB_CHECK_TRUE(n == 2 || n == 3); /* rounding may push it to 3 */
        CB_CHECK_OK(cb_ft_max_posterior(&q2, n, &q));
        CB_CHECK_TRUE(q >= q2.threshold);
        if (n > 0) {
            CB_CHECK_OK(cb_ft_max_posterior(&q2, n - 1, &q));
            CB_CHECK_TRUE(q < q2.threshold);
        }
    }

    CB_CASE("a bigger cap needs fewer sources, a smaller cap needs more");
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.lr_cap = 100.0;
    CB_CHECK_OK(cb_ft_min_sources(&p, &n));
    CB_CHECK_EQ_INT(n, 1);
    p.lr_cap = 1.5;
    CB_CHECK_OK(cb_ft_min_sources(&p, &n));
    CB_CHECK_EQ_INT(n, 7);
    CB_CHECK_OK(cb_ft_max_posterior(&p, 6, &q));
    CB_CHECK_TRUE(q < p.threshold);
    CB_CHECK_OK(cb_ft_max_posterior(&p, 7, &q));
    CB_CHECK_TRUE(q >= p.threshold);

    CB_CASE("a lower trust weight needs more sources");
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.max_weight = 0.5;
    CB_CHECK_OK(cb_ft_min_sources(&p, &n));
    CB_CHECK_EQ_INT(n, 3);

    CB_CASE("a prior already above threshold needs zero sources");
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.prior = 0.5;
    p.threshold = 0.2;
    CB_CHECK_OK(cb_ft_min_sources(&p, &n));
    CB_CHECK_EQ_INT(n, 0);
    CB_CHECK_OK(cb_ft_check_invariant(&p, 0, &safe));
    CB_CHECK_EQ_INT(safe, 0);

    CB_CASE("fault-tolerance domain errors");
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.lr_cap = 1.0;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.threshold = 0.0;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    p.threshold = 1.0;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.max_weight = 0.0;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    p.max_weight = 1.5;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.odds_multiplier = 0.0;
    CB_CHECK_STATUS(cb_ft_min_sources(&p, &n), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_ft_params_init(&p));
    CB_CHECK_STATUS(cb_ft_max_posterior(&p, -1, &q), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_ft_min_sources(&p, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_ft_min_sources(NULL, &n), CB_ERR_NULL);

    CB_CASE("a cap barely above one is a capacity error, not a hang");
    CB_CHECK_OK(cb_ft_params_init(&p));
    p.lr_cap = 1.0 + 1e-300;
    CB_CHECK_TRUE(cb_ft_min_sources(&p, &n) != CB_OK);
}

int main(void)
{
    test_logit_logistic();
    test_worked_example();
    test_cap_and_weights();
    test_group_capping();
    test_fusion_domain_errors();
    test_large_n();
    test_ft_inequality();
    return cb_t_report("test_fusion");
}
