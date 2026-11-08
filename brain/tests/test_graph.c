/* Correlation graph: edge bounds, and the composition floor proof in code. */
#include "cb_test.h"

static void test_init_and_edges(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    cb_edge_kind_t kind;
    double m;

    CB_CASE("an empty graph is all ones and no edges");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_FLOOD, CB_EXAMPLE_FIRE, &kind, &m));
    CB_CHECK_EQ_INT(kind, CB_EDGE_NONE);
    CB_CHECK_NEAR(m, 1.0, 0.0);

    CB_CASE("uninitialised graph is a state error");
    {
        cb_graph_t raw;
        memset(&raw, 0, sizeof raw);
        CB_CHECK_STATUS(cb_graph_get_edge(&raw, 0, 1, &kind, &m), CB_ERR_STATE);
        CB_CHECK_STATUS(cb_graph_set_edge(&raw, 0, 1, CB_EDGE_RAISES, 2.0),
                        CB_ERR_STATE);
    }

    CB_CASE("config domains are checked");
    CB_CHECK_OK(cb_graph_config_init(&cfg)); cfg.m_min = 0.0;
    CB_CHECK_STATUS(cb_graph_init(&g, &cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_graph_config_init(&cfg)); cfg.m_min = 1.5;
    CB_CHECK_STATUS(cb_graph_init(&g, &cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_graph_config_init(&cfg)); cfg.m_max = 0.9;
    CB_CHECK_STATUS(cb_graph_init(&g, &cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_graph_config_init(&cfg)); cfg.n_nodes = 0;
    CB_CHECK_STATUS(cb_graph_init(&g, &cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    cfg.n_nodes = CB_GRAPH_MAX_NODES + 1;
    CB_CHECK_STATUS(cb_graph_init(&g, &cfg), CB_ERR_DOMAIN);

    CB_CASE("edge kind and multiplier must agree");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 1, CB_EDGE_RAISES, 0.5), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 1, CB_EDGE_LOWERS, 2.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 1, CB_EDGE_NONE, 1.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 1, CB_EDGE_RAISES, -1.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 1, CB_EDGE_RAISES, (double)NAN),
                    CB_ERR_NOT_FINITE);

    CB_CASE("self-loops are rejected");
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 2, 2, CB_EDGE_RAISES, 2.0), CB_ERR_DOMAIN);

    CB_CASE("node indices are range checked");
    CB_CHECK_STATUS(cb_graph_set_edge(&g, -1, 1, CB_EDGE_RAISES, 2.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_set_edge(&g, 0, 99, CB_EDGE_RAISES, 2.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_get_edge(&g, 0, 99, &kind, &m), CB_ERR_DOMAIN);

    CB_CASE("a stored edge is clipped to the configured bounds");
    CB_CHECK_OK(cb_graph_set_edge(&g, 0, 1, CB_EDGE_RAISES, 1000.0));
    CB_CHECK_OK(cb_graph_get_edge(&g, 0, 1, &kind, &m));
    CB_CHECK_NEAR(m, cfg.m_max, 0.0);
    CB_CHECK_OK(cb_graph_set_edge(&g, 1, 0, CB_EDGE_LOWERS, 1e-9));
    CB_CHECK_OK(cb_graph_get_edge(&g, 1, 0, &kind, &m));
    CB_CHECK_NEAR(m, cfg.m_min, 0.0);

    CB_CASE("clearing an edge restores the neutral multiplier");
    CB_CHECK_OK(cb_graph_clear_edge(&g, 0, 1));
    CB_CHECK_OK(cb_graph_get_edge(&g, 0, 1, &kind, &m));
    CB_CHECK_EQ_INT(kind, CB_EDGE_NONE);
    CB_CHECK_NEAR(m, 1.0, 0.0);
}

static void test_example_edges(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    cb_edge_kind_t kind;
    double m;

    CB_CASE("the example edge set loads as documented");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_OK(cb_example_load_graph(&g));

    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_ACCIDENT, CB_EXAMPLE_JAM, &kind, &m));
    CB_CHECK_EQ_INT(kind, CB_EDGE_RAISES);
    CB_CHECK_NEAR(m, 3.0, 0.0);
    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_FLOOD, CB_EXAMPLE_ACCIDENT, &kind, &m));
    CB_CHECK_NEAR(m, 1.5, 0.0);
    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_FLOOD, CB_EXAMPLE_JAM, &kind, &m));
    CB_CHECK_NEAR(m, 1.3, 0.0);
    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_FIRE, CB_EXAMPLE_JAM, &kind, &m));
    CB_CHECK_NEAR(m, 1.5, 0.0);
    CB_CHECK_OK(cb_graph_get_edge(&g, CB_EXAMPLE_FLOOD, CB_EXAMPLE_FIRE, &kind, &m));
    CB_CHECK_EQ_INT(kind, CB_EDGE_LOWERS);
    CB_CHECK_NEAR(m, 0.5, 0.0);
}

static void test_floor_under_composition(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    int32_t path[CB_GRAPH_MAX_NODES];
    double m;
    int32_t i, j;

    CB_CASE("a single lowers edge cannot go below the floor");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    cfg.n_nodes = CB_GRAPH_MAX_NODES;
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    for (i = 0; i + 1 < cfg.n_nodes; ++i) {
        CB_CHECK_OK(cb_graph_set_edge(&g, i, i + 1, CB_EDGE_LOWERS, cfg.m_min));
    }
    path[0] = 0; path[1] = 1;
    CB_CHECK_OK(cb_graph_path_multiplier(&g, path, 2, &m));
    CB_CHECK_NEAR(m, cfg.m_min, 0.0);

    CB_CASE("a chain of seven lowers edges still cannot go below the floor");
    for (i = 0; i < cfg.n_nodes; ++i) { path[i] = i; }
    for (j = 2; j <= cfg.n_nodes; ++j) {
        CB_CHECK_OK(cb_graph_path_multiplier(&g, path, (size_t)j, &m));
        CB_CHECK_TRUE(m >= cfg.m_min);
        CB_CHECK_TRUE(m <= cfg.m_max);
        /* The raw product would be m_min^(j-1), far below the floor. */
        CB_CHECK_NEAR(m, cfg.m_min, 0.0);
    }
    CB_CHECK_TRUE(pow(cfg.m_min, (double)(cfg.n_nodes - 1)) < cfg.m_min);

    CB_CASE("the ceiling holds under composition too");
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    for (i = 0; i + 1 < cfg.n_nodes; ++i) {
        CB_CHECK_OK(cb_graph_set_edge(&g, i, i + 1, CB_EDGE_RAISES, cfg.m_max));
    }
    for (j = 2; j <= cfg.n_nodes; ++j) {
        CB_CHECK_OK(cb_graph_path_multiplier(&g, path, (size_t)j, &m));
        CB_CHECK_NEAR(m, cfg.m_max, 0.0);
    }

    CB_CASE("a mixed chain stays inside the interval");
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    for (i = 0; i + 1 < cfg.n_nodes; ++i) {
        if ((i % 2) == 0) {
            CB_CHECK_OK(cb_graph_set_edge(&g, i, i + 1, CB_EDGE_LOWERS, 0.7));
        } else {
            CB_CHECK_OK(cb_graph_set_edge(&g, i, i + 1, CB_EDGE_RAISES, 2.0));
        }
    }
    for (j = 2; j <= cfg.n_nodes; ++j) {
        CB_CHECK_OK(cb_graph_path_multiplier(&g, path, (size_t)j, &m));
        CB_CHECK_TRUE(m >= cfg.m_min && m <= cfg.m_max);
    }

    CB_CASE("path edge cases");
    CB_CHECK_OK(cb_graph_path_multiplier(&g, path, 1, &m));
    CB_CHECK_NEAR(m, 1.0, 0.0);
    CB_CHECK_OK(cb_graph_path_multiplier(&g, NULL, 0, &m));
    CB_CHECK_NEAR(m, 1.0, 0.0);
    CB_CHECK_STATUS(cb_graph_path_multiplier(&g, path, CB_GRAPH_MAX_NODES + 1, &m),
                    CB_ERR_CAPACITY);
    path[0] = 99;
    CB_CHECK_STATUS(cb_graph_path_multiplier(&g, path, 2, &m), CB_ERR_DOMAIN);
}

static void test_extremal(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    double lo, hi;

    CB_CASE("extremal multiplier over all simple paths respects both bounds");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_OK(cb_example_load_graph(&g));

    /* Three simple paths from FLOOD to JAM in the example graph:
     *   FLOOD -> JAM                 1.3
     *   FLOOD -> ACCIDENT -> JAM     1.5 * 3.0 = 4.5, clipped to m_max = 3.0
     *   FLOOD -> FIRE -> JAM         0.5 * 1.5 = 0.75
     * so the extremes are 0.75 and 3.0. */
    CB_CHECK_OK(cb_graph_extremal_multiplier(&g, CB_EXAMPLE_FLOOD,
                                             CB_EXAMPLE_JAM, &lo, &hi));
    CB_CHECK_NEAR(lo, 0.75, 1e-15);
    CB_CHECK_NEAR(hi, cfg.m_max, 0.0);
    CB_CHECK_TRUE(lo >= cfg.m_min && hi <= cfg.m_max);

    CB_CASE("no path reports NOT_FOUND and leaves a neutral multiplier");
    CB_CHECK_OK(cb_graph_extremal_multiplier(&g, CB_EXAMPLE_FLOOD,
                                             CB_EXAMPLE_ACCIDENT, &lo, &hi));
    CB_CHECK_STATUS(cb_graph_extremal_multiplier(&g, CB_EXAMPLE_JAM,
                        CB_EXAMPLE_FLOOD, &lo, &hi), CB_ERR_NOT_FOUND);
    CB_CHECK_NEAR(lo, 1.0, 0.0);
    CB_CHECK_NEAR(hi, 1.0, 0.0);

    CB_CASE("extremal rejects a self query and bad indices");
    CB_CHECK_STATUS(cb_graph_extremal_multiplier(&g, 1, 1, &lo, &hi), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_extremal_multiplier(&g, 1, 99, &lo, &hi), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_graph_extremal_multiplier(&g, CB_EXAMPLE_FLOOD,
                                             CB_EXAMPLE_JAM, NULL, NULL));
}

static void test_adjust_prior(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    int32_t active[8];
    double adjusted, mult;

    CB_CASE("an active source category raises the prior on its target");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_OK(cb_example_load_graph(&g));

    active[0] = CB_EXAMPLE_ACCIDENT;
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 1, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_NEAR(mult, 3.0, 0.0);
    CB_CHECK_NEAR(adjusted, (0.02 / 0.98 * 3.0) / (1.0 + 0.02 / 0.98 * 3.0), 1e-14);
    CB_CHECK_TRUE(adjusted > 0.02);

    CB_CASE("a lowering edge reduces the prior, but only to the floor");
    active[0] = CB_EXAMPLE_FLOOD;
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_FIRE, active, 1, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_NEAR(mult, 0.5, 0.0);
    CB_CHECK_TRUE(adjusted < 0.02);
    CB_CHECK_TRUE(mult >= cfg.m_min);

    CB_CASE("repeats in the active list contribute once");
    active[0] = CB_EXAMPLE_ACCIDENT;
    active[1] = CB_EXAMPLE_ACCIDENT;
    active[2] = CB_EXAMPLE_ACCIDENT;
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 3, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_NEAR(mult, 3.0, 0.0);

    CB_CASE("the order of the active list does not change the result");
    {
        double m1, m2, a1, a2;
        active[0] = CB_EXAMPLE_ACCIDENT;
        active[1] = CB_EXAMPLE_FLOOD;
        active[2] = CB_EXAMPLE_FIRE;
        CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 3, 0.02, &a1, &m1));
        active[0] = CB_EXAMPLE_FIRE;
        active[1] = CB_EXAMPLE_ACCIDENT;
        active[2] = CB_EXAMPLE_FLOOD;
        CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 3, 0.02, &a2, &m2));
        CB_CHECK_NEAR(m1, m2, 0.0);
        CB_CHECK_NEAR(a1, a2, 0.0);
        /* 3 * 1.3 * 1.5 = 5.85, clipped to the ceiling */
        CB_CHECK_NEAR(m1, cfg.m_max, 0.0);
    }

    CB_CASE("a category does not adjust its own prior");
    active[0] = CB_EXAMPLE_JAM;
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 1, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_NEAR(mult, 1.0, 0.0);
    CB_CHECK_NEAR(adjusted, 0.02, 1e-15);

    CB_CASE("an empty active set leaves the prior alone");
    CB_CHECK_OK(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, NULL, 0, 0.02,
                                      &adjusted, &mult));
    CB_CHECK_NEAR(mult, 1.0, 0.0);
    CB_CHECK_NEAR(adjusted, 0.02, 1e-15);

    CB_CASE("adjust_prior argument domains");
    active[0] = CB_EXAMPLE_FLOOD;
    CB_CHECK_STATUS(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 1, 0.0,
                                          &adjusted, &mult), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 1, 1.0,
                                          &adjusted, &mult), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_adjust_prior(&g, 99, active, 1, 0.02,
                                          &adjusted, &mult), CB_ERR_DOMAIN);
    active[0] = 42;
    CB_CHECK_STATUS(cb_graph_adjust_prior(&g, CB_EXAMPLE_JAM, active, 1, 0.02,
                                          &adjusted, &mult), CB_ERR_DOMAIN);
}

static void test_unrelated_pair(void)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    int32_t active[4];
    int32_t flag;

    CB_CASE("a set with fewer than two categories reports nothing");
    CB_CHECK_OK(cb_graph_config_init(&cfg));
    CB_CHECK_OK(cb_graph_init(&g, &cfg));
    CB_CHECK_OK(cb_example_load_graph(&g));
    active[0] = CB_EXAMPLE_FLOOD;
    CB_CHECK_OK(cb_graph_has_unrelated_pair(&g, active, 1, &flag));
    CB_CHECK_EQ_INT(flag, 0);

    CB_CASE("two categories joined by an edge are related");
    active[1] = CB_EXAMPLE_FIRE;
    CB_CHECK_OK(cb_graph_has_unrelated_pair(&g, active, 2, &flag));
    CB_CHECK_EQ_INT(flag, 0);

    CB_CASE("three categories where one pair has no edge reports a pair");
    active[2] = CB_EXAMPLE_ACCIDENT;
    CB_CHECK_OK(cb_graph_has_unrelated_pair(&g, active, 3, &flag));
    CB_CHECK_EQ_INT(flag, 1);

    CB_CASE("unrelated-pair argument domains");
    active[0] = 77;
    CB_CHECK_STATUS(cb_graph_has_unrelated_pair(&g, active, 1, &flag),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_graph_has_unrelated_pair(&g, NULL, 0, NULL),
                    CB_ERR_NULL);
}

int main(void)
{
    test_init_and_edges();
    test_example_edges();
    test_floor_under_composition();
    test_extremal();
    test_adjust_prior();
    test_unrelated_pair();
    return cb_t_report("test_graph");
}
