/*
 * Adversarial tests for the bounded-influence guarantee: given a caller
 * that hands the library maximum weight on fabricated evidence, does the
 * cap still bound the posterior and does the fault-tolerance bound still
 * predict the exact number of sources needed to cross a threshold?
 */
#include "cb_test.h"

/* 1. A fully compromised caller: maximum weight on fabricated evidence */
static void adversary_compromised_weights(void)
{
    static const double caps[8] = { 1.5, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 50.0 };
    static const double priors[4] = { 0.005, 0.01, 0.02, 0.05 };
    static const double thresholds[3] = { 0.10, 0.20, 0.35 };
    size_t ci, pi, ti;
    int crossings = 0;

    CB_CASE("one fabricated source at full weight cannot cross on its own");
    for (ci = 0; ci < 8; ++ci) {
        for (pi = 0; pi < 4; ++pi) {
            for (ti = 0; ti < 3; ++ti) {
                cb_fusion_config_t cfg;
                cb_fusion_result_t res;
                cb_ft_params_t ft;
                cb_evidence_t item;
                int32_t n_min;

                CB_CHECK_OK(cb_fusion_config_init(&cfg));
                cfg.lr_cap = caps[ci];
                cfg.prior = priors[pi];

                CB_CHECK_OK(cb_ft_params_init(&ft));
                ft.lr_cap = caps[ci];
                ft.prior = priors[pi];
                ft.threshold = thresholds[ti];
                CB_CHECK_OK(cb_ft_min_sources(&ft, &n_min));

                /* The most extreme claim a double can carry, at full trust. */
                item.likelihood_ratio = 1e300;
                item.weight = 1.0;
                item.group_id = 0;
                CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, &item, 1, &res));

                if (n_min > 1) {
                    CB_CHECK_TRUE(res.posterior < thresholds[ti]);
                    if (res.posterior >= thresholds[ti]) { crossings++; }
                }
            }
        }
    }
    CB_CHECK_EQ_INT(crossings, 0);
    printf("  single full-weight fabricated source: 0 threshold crossings "
           "over %d (cap, prior, threshold) combinations\n", 8 * 4 * 3);

    CB_CASE("the worked example, restated exactly");
    {
        cb_fusion_config_t cfg;
        cb_fusion_result_t res;
        cb_evidence_t item;
        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        item.likelihood_ratio = 1e300;
        item.weight = 1.0;
        item.group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, &item, 1, &res));
        CB_CHECK_NEAR(res.posterior, 0.1403508771929825, 1e-15);
        CB_CHECK_TRUE(res.posterior < 0.20);
        printf("  cap 8x, prior 2%%: one full-weight source reaches %.4f, "
               "threshold 0.2000\n", res.posterior);
    }

    CB_CASE("full weight everywhere is no better than the cap allows");
    {
        /* The cap binds regardless of weight; lower weights only reduce influence. */
        cb_fusion_config_t cfg;
        cb_fusion_result_t full, partial;
        cb_evidence_t items[6];
        size_t i;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        for (i = 0; i < 6; ++i) {
            items[i].likelihood_ratio = 1e300;
            items[i].weight = 1.0;
            items[i].group_id = (int32_t)i;
        }
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 6, &full));
        for (i = 0; i < 6; ++i) { items[i].weight = 0.4; }
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 6, &partial));
        CB_CHECK_TRUE(partial.posterior <= full.posterior);
        CB_CHECK_NEAR(full.evidence_log_odds, 6.0 * log(cfg.lr_cap), 1e-14);
    }
}

/* 2. Coordinated fabrication across many sources */
static void adversary_coordinated_fabrication(void)
{
    static const double caps[5] = { 1.5, 2.0, 4.0, 8.0, 15.0 };
    static const double priors[3] = { 0.001, 0.02, 0.10 };
    static const double thresholds[3] = { 0.15, 0.20, 0.50 };
    static const double weights[3] = { 0.25, 0.6, 1.0 };
    size_t ci, pi, ti, wi;
    int checked = 0;

    CB_CASE("the number of fabricated groups needed matches the derived bound");
    for (ci = 0; ci < 5; ++ci) {
    for (pi = 0; pi < 3; ++pi) {
    for (ti = 0; ti < 3; ++ti) {
    for (wi = 0; wi < 3; ++wi) {
        cb_fusion_config_t cfg;
        cb_ft_params_t ft;
        cb_fusion_result_t res;
        cb_evidence_t items[64];
        int32_t n_min, k;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        cfg.lr_cap = caps[ci];
        cfg.prior = priors[pi];
        CB_CHECK_OK(cb_ft_params_init(&ft));
        ft.lr_cap = caps[ci];
        ft.prior = priors[pi];
        ft.threshold = thresholds[ti];
        ft.max_weight = weights[wi];
        if (cb_ft_min_sources(&ft, &n_min) != CB_OK) { continue; }
        if (n_min < 1 || n_min > 60) { continue; }

        for (k = 0; k < 64; ++k) {
            items[k].likelihood_ratio = 1e300; /* clipped to the cap */
            items[k].weight = weights[wi];
            items[k].group_id = k;
        }

        /* One group short of the bound: must not reach the threshold. */
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items,
                                                    (size_t)(n_min - 1), &res));
        CB_CHECK_TRUE(res.posterior < thresholds[ti]);

        /* Exactly the bound: must reach it. */
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items,
                                                    (size_t)n_min, &res));
        CB_CHECK_TRUE(res.posterior >= thresholds[ti]);
        checked++;
    }}}}
    printf("  coordinated fabrication: bound confirmed tight in %d "
           "configurations\n", checked);
    CB_CHECK_TRUE(checked > 50);

    CB_CASE("a thousand fabricated items in one group are still one group");
    {
        cb_fusion_config_t cfg;
        cb_fusion_result_t res;
        static cb_evidence_t items[1000];
        static double scratch[4];
        size_t i;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        for (i = 0; i < 1000; ++i) {
            items[i].likelihood_ratio = 1e300;
            items[i].weight = 1.0;
            items[i].group_id = 0;
        }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 1000, scratch, 4, &res));
        CB_CHECK_NEAR(res.posterior, 0.1403508771929825, 1e-15);
        CB_CHECK_TRUE(res.posterior < 0.20);
        CB_CHECK_EQ_INT(res.n_groups_used, 1);
        printf("  1000 items in one group reach %.4f, the same as one item\n",
               res.posterior);
    }

    CB_CASE("influence grows by exactly one cap per additional group");
    {
        cb_fusion_config_t cfg;
        cb_fusion_result_t a, b;
        cb_evidence_t items[4];
        double scratch[4];
        size_t i;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        for (i = 0; i < 4; ++i) {
            items[i].likelihood_ratio = 8.0;
            items[i].weight = 1.0;
            items[i].group_id = (int32_t)(i / 2); /* two groups of two */
        }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 4, scratch, 4, &a));
        CB_CHECK_EQ_INT(a.n_groups_used, 2);
        CB_CHECK_NEAR(a.evidence_log_odds, 2.0 * log(8.0), 1e-14);

        for (i = 0; i < 4; ++i) { items[i].group_id = (int32_t)i; }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 4, scratch, 4, &b));
        CB_CHECK_NEAR(b.evidence_log_odds, 4.0 * log(8.0), 1e-14);
        CB_CHECK_TRUE(b.posterior > a.posterior);
    }

    CB_CASE("mixing fabricated and genuine evidence does not break the ceiling");
    {
        cb_fusion_config_t cfg;
        cb_fusion_result_t res;
        cb_ft_params_t ft;
        cb_evidence_t items[10];
        double scratch[10];
        double ceiling;
        size_t i;

        CB_CHECK_OK(cb_fusion_config_init(&cfg));
        for (i = 0; i < 10; ++i) {
            /* alternating fabricated extremes and ordinary weak evidence */
            items[i].likelihood_ratio = (i % 2 == 0) ? 1e300 : 1.4;
            items[i].weight = (i % 2 == 0) ? 1.0 : 0.3;
            items[i].group_id = (int32_t)(i % 5);
        }
        CB_CHECK_OK(cb_fusion_posterior(&cfg, items, 10, scratch, 10, &res));
        CB_CHECK_OK(cb_ft_params_init(&ft));
        ft.lr_cap = cfg.lr_cap;
        ft.prior = cfg.prior;
        ft.max_weight = 1.0;
        CB_CHECK_OK(cb_ft_max_posterior(&ft, res.n_groups_used, &ceiling));
        CB_CHECK_TRUE(res.posterior <= ceiling + 1e-15);
        CB_CHECK_EQ_INT(res.n_groups_used, 5);
    }
}

/* 3. Hostile weights: the one thing a compromised caller controls */
static void adversary_hostile_weights(void)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t item;

    CB_CASE("a weight above 1 is rejected, not scaled up");
    CB_CHECK_OK(cb_fusion_config_init(&cfg));
    item.likelihood_ratio = 8.0;
    item.weight = 100.0;
    item.group_id = 0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, &item, 1, &res),
                    CB_ERR_DOMAIN);

    CB_CASE("a negative weight would invert evidence; also rejected");
    item.weight = -1.0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, &item, 1, &res),
                    CB_ERR_DOMAIN);

    CB_CASE("a non-finite weight is rejected");
    item.weight = (double)NAN;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, &item, 1, &res),
                    CB_ERR_NOT_FINITE);
    item.weight = HUGE_VAL;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, &item, 1, &res),
                    CB_ERR_NOT_FINITE);

    CB_CASE("the cap binds before the weight is applied, not after");
    item.weight = 1.0;
    item.likelihood_ratio = 1e308;
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, &item, 1, &res));
    CB_CHECK_NEAR(res.evidence_log_odds, log(cfg.lr_cap), 1e-15);

    CB_CASE("a likelihood ratio of zero is a defined error, not minus infinity");
    item.likelihood_ratio = 0.0;
    CB_CHECK_STATUS(cb_fusion_posterior_independent(&cfg, &item, 1, &res),
                    CB_ERR_DOMAIN);
}

/* 4. Suppression: can a lowering multiplier mute strong evidence? */
static void adversary_suppression(void)
{
    cb_graph_config_t gcfg;
    cb_graph_t g;
    cb_fusion_config_t fcfg;
    cb_fusion_result_t res;
    cb_ft_params_t ft_plain, ft_suppressed;
    cb_evidence_t items[16];
    int32_t active[4];
    double adjusted, mult;
    int32_t n_plain, n_suppressed;
    int32_t k;
    const double threshold = 0.20;

    CB_CASE("a lowering edge reduces the prior, but only to the floor");
    CB_CHECK_OK(cb_graph_config_init(&gcfg));
    CB_CHECK_OK(cb_graph_init(&g, &gcfg));
    CB_CHECK_OK(cb_example_load_graph(&g));

    active[0] = CB_EXAMPLE_FLOOD;
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_FIRE, active, 1, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_TRUE(mult >= gcfg.m_min);
    CB_CHECK_NEAR(mult, 0.5, 0.0);

    CB_CASE("maximum suppression costs at most a bounded number of extra sources");
    CB_CHECK_OK(cb_ft_params_init(&ft_plain));
    ft_plain.threshold = threshold;
    CB_CHECK_OK(cb_ft_min_sources(&ft_plain, &n_plain));

    ft_suppressed = ft_plain;
    ft_suppressed.odds_multiplier = gcfg.m_min; /* the worst the floor allows */
    CB_CHECK_OK(cb_ft_min_sources(&ft_suppressed, &n_suppressed));
    CB_CHECK_TRUE(n_suppressed >= n_plain);
    CB_CHECK_TRUE(n_suppressed - n_plain <=
                  (int32_t)ceil(log(1.0 / gcfg.m_min) / log(ft_plain.lr_cap)) + 1);
    printf("  suppression: %d sources needed normally, %d under maximum "
           "suppression\n", n_plain, n_suppressed);

    CB_CASE("corroborated evidence still crosses the threshold under suppression");
    CB_CHECK_OK(cb_fusion_config_init(&fcfg));
    fcfg.prior = adjusted;
    for (k = 0; k < 16; ++k) {
        items[k].likelihood_ratio = fcfg.lr_cap;
        items[k].weight = 1.0;
        items[k].group_id = k;
    }
    CB_CHECK_OK(cb_fusion_posterior_independent(&fcfg, items,
                                                (size_t)n_suppressed, &res));
    CB_CHECK_TRUE(res.posterior >= threshold);

    CB_CASE("no chain of lowering edges suppresses further than the floor");
    {
        cb_graph_config_t c2;
        cb_graph_t g2;
        int32_t all[CB_GRAPH_MAX_NODES];
        int32_t i;
        double m2;

        CB_CHECK_OK(cb_graph_config_init(&c2));
        c2.n_nodes = CB_GRAPH_MAX_NODES;
        CB_CHECK_OK(cb_graph_init(&g2, &c2));
        for (i = 1; i < c2.n_nodes; ++i) {
            CB_CHECK_OK(cb_graph_set_edge(&g2, i, 0, CB_EDGE_LOWERS, c2.m_min));
            all[i - 1] = i;
        }
        CB_CHECK_OK(cb_graph_adjust_prior(&g2, 0, all, (size_t)(c2.n_nodes - 1),
                                          0.02, &adjusted, &m2));
        CB_CHECK_NEAR(m2, c2.m_min, 0.0);
        CB_CHECK_TRUE(m2 >= c2.m_min);
        CB_CHECK_TRUE(pow(c2.m_min, 7.0) < c2.m_min * 0.02);
        printf("  seven stacked lowering edges: applied multiplier %.4f, "
               "unclipped product would be %.6f\n", m2, pow(c2.m_min, 7.0));
    }

    CB_CASE("suppression can never make the required source count unbounded");
    {
        cb_ft_params_t p;
        int32_t n;
        int i;
        for (i = 1; i <= 10; ++i) {
            CB_CHECK_OK(cb_ft_params_init(&p));
            p.threshold = threshold;
            p.odds_multiplier = gcfg.m_min * (double)i / 10.0;
            if (p.odds_multiplier <= 0.0) { continue; }
            CB_CHECK_OK(cb_ft_min_sources(&p, &n));
            CB_CHECK_TRUE(n > 0 && n < 100);
        }
    }
}

/* Physics: one physically absurd reading per incident type. Confirms the
 * physics functions never crash or return NaN, and that an absurd reading
 * comes back as a low likelihood ratio, never a rejected/flagged status. */
static void adversary_physics_absurd_readings(void)
{
    double expected_rate, lr;

    CB_CASE("flood: a physically impossible instantaneous drop is weak "
            "evidence for a flood, not a crash or a rejected reading");
    /* real flood hypothesis predicts ~0.0996 m/s rise; a 500 m/s drop is impossible */
    CB_CHECK_OK(cb_physics_flood_rise_rate(500.0, 50.0, 0.2, 1.5, 1.0, &expected_rate));
    CB_CHECK_NEAR(expected_rate, 0.0996, 1e-9);
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(-500.0, expected_rate, 0.0,
                                                      0.05, &lr));
    CB_CHECK_TRUE(lr >= 0.0 && !isnan(lr));
    CB_CHECK_TRUE(lr < 1e-6); /* a "low evidentiary weight" outcome */

    CB_CASE("fire: clean air despite a severe nearby fire hypothesis is "
            "weak evidence for fire, not a crash or a rejected reading");
    {
        /* ultrafast fire, 300s in, close range: clean air is implausible if real */
        double alpha, hrr, conc_h1, fire_lr;
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_ULTRAFAST, &alpha));
        CB_CHECK_OK(cb_physics_fire_heat_release_rate(alpha, 300.0, &hrr));
        CB_CHECK_OK(cb_physics_plume_concentration(0.1 * hrr, 2.0, 20.0, 10.0,
                                                    0.0, 0.0, 0.0, &conc_h1));
        CB_CHECK_TRUE(conc_h1 > 0.0);
        CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(0.0, conc_h1, 0.0,
                                                          conc_h1 * 0.2 > 0.0
                                                              ? conc_h1 * 0.2 : 0.01,
                                                          &fire_lr));
        CB_CHECK_TRUE(fire_lr >= 0.0 && !isnan(fire_lr));
        CB_CHECK_TRUE(fire_lr < 1e-4); /* a "low evidentiary weight" outcome */
    }

    CB_CASE("traffic: an impossibly high speed is weak evidence for "
            "congestion, not a crash or a rejected reading");
    {
        /* near-jam density predicts ~2.5 m/s; 300 m/s is impossible for real traffic */
        double expected_jam_speed, traffic_lr;
        CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 110.0,
                                                           &expected_jam_speed));
        CB_CHECK_NEAR(expected_jam_speed, 2.5, 1e-9);
        CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(300.0, expected_jam_speed,
                                                          30.0, 1.0, &traffic_lr));
        CB_CHECK_TRUE(traffic_lr >= 0.0 && !isnan(traffic_lr));
        CB_CHECK_TRUE(traffic_lr < 1e-6); /* a "low evidentiary weight" outcome */
    }

    /* Known limitation: an overshoot in the SAME direction as the hypothesis
     * can swing the ratio high instead of low (see docs/math_reasoning.md).
     * Still never crashes or returns NaN. */
    CB_CASE("documented limitation: overshoot in the hypothesis direction");
    {
        double alpha, hrr, conc_h1, fire_lr;
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_ULTRAFAST, &alpha));
        CB_CHECK_OK(cb_physics_fire_heat_release_rate(alpha, 300.0, &hrr));
        CB_CHECK_OK(cb_physics_plume_concentration(0.1 * hrr, 2.0, 20.0, 10.0,
                                                    0.0, 0.0, 0.0, &conc_h1));
        CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(1e6, conc_h1, 0.0,
                                                          conc_h1 * 0.2, &fire_lr));
        CB_CHECK_TRUE(!isnan(fire_lr)); /* never garbage, even here */
        CB_CHECK_TRUE(fire_lr > 1.0);   /* the documented swing-the-wrong-way case */
    }
}

int main(void)
{
    adversary_compromised_weights();
    adversary_coordinated_fabrication();
    adversary_hostile_weights();
    adversary_suppression();
    adversary_physics_absurd_readings();
    return cb_t_report("test_adversarial");
}
