/* Golden-vector runner. Reads tests/vectors/golden.csv, generated
 * independently in Python from the mathematical definitions, and checks the
 * C library's output against it; the small CSV parser below just splits on
 * commas, skips comment/blank/section lines, and reads a header row per section. */
#include "cb_test.h"

#ifndef CB_VECTORS_PATH
#define CB_VECTORS_PATH "vectors/golden.csv"
#endif

#define MAX_LINE 4096
#define MAX_FIELDS 16

typedef struct {
    char *fields[MAX_FIELDS];
    int n_fields;
} row_t;

static char *trim_newline(char *s)
{
    size_t n = strlen(s);
    while (n > 0 && (s[n-1] == '\n' || s[n-1] == '\r')) { s[--n] = '\0'; }
    return s;
}

static int split_csv(char *line, char **fields, int max_fields)
{
    int n = 0;
    char *tok = strtok(line, ",");
    while (tok != NULL && n < max_fields) {
        fields[n++] = tok;
        tok = strtok(NULL, ",");
    }
    return n;
}

static double col_d(row_t *r, int i) { return atof(r->fields[i]); }
static int32_t col_i(row_t *r, int i) { return (int32_t)atol(r->fields[i]); }
static uint64_t col_u64(row_t *r, int i) { return strtoull(r->fields[i], NULL, 10); }
static const char *col_s(row_t *r, int i) { return r->fields[i]; }

/* Column index lookup within a header row. */
static int find_col(row_t *header, const char *name)
{
    int i;
    for (i = 0; i < header->n_fields; ++i) {
        if (strcmp(header->fields[i], name) == 0) { return i; }
    }
    fprintf(stderr, "golden.csv: column '%s' not found\n", name);
    exit(2);
}

static void check_fusion_row(row_t *h, row_t *r)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    static cb_evidence_t items[1]; /* vectors use at most one representative item count check below */
    double lr_cap = col_d(r, find_col(h, "lr_cap"));
    double prior = col_d(r, find_col(h, "prior"));
    double expected_post = col_d(r, find_col(h, "posterior"));
    double expected_evid = col_d(r, find_col(h, "evidence_log_odds"));
    const char *name = col_s(r, find_col(h, "name"));

    /* Reconstruct the evidence array for each named case from the CSV's summary columns. */
    CB_CHECK_OK(cb_fusion_config_init(&cfg));
    cfg.lr_cap = lr_cap;
    cfg.prior = prior;

    if (strcmp(name, "empty") == 0) {
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, NULL, 0, &res));
    } else if (strcmp(name, "one_item_lr_equal_cap") == 0 ||
               strcmp(name, "brief_worked_example_one_source") == 0) {
        items[0].likelihood_ratio = (strcmp(name, "one_item_lr_equal_cap") == 0) ? 8.0 : 1e300;
        items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "one_item_above_cap_clips") == 0) {
        items[0].likelihood_ratio = 1e30; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "one_item_below_floor_clips") == 0) {
        items[0].likelihood_ratio = 1e-30; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "weighted_half") == 0) {
        items[0].likelihood_ratio = 8.0; items[0].weight = 0.5; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "weight_zero_silences") == 0) {
        items[0].likelihood_ratio = 1e9; items[0].weight = 0.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "lr_one_no_effect") == 0) {
        items[0].likelihood_ratio = 1.0; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "two_cancel") == 0) {
        cb_evidence_t two[2];
        two[0].likelihood_ratio = 4.0; two[0].weight = 1.0; two[0].group_id = 0;
        two[1].likelihood_ratio = 0.25; two[1].weight = 1.0; two[1].group_id = 1;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, two, 2, &res));
    } else if (strcmp(name, "brief_worked_example_two_sources") == 0) {
        cb_evidence_t two[2];
        two[0].likelihood_ratio = 1e300; two[0].weight = 1.0; two[0].group_id = 0;
        two[1].likelihood_ratio = 1e300; two[1].weight = 1.0; two[1].group_id = 1;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, two, 2, &res));
    } else if (strcmp(name, "three_mixed") == 0) {
        cb_evidence_t three[3];
        three[0].likelihood_ratio = 3.0; three[0].weight = 1.0; three[0].group_id = 0;
        three[1].likelihood_ratio = 0.4; three[1].weight = 0.5; three[1].group_id = 1;
        three[2].likelihood_ratio = 10.0; three[2].weight = 0.25; three[2].group_id = 2;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, three, 3, &res));
    } else if (strcmp(name, "high_prior") == 0) {
        items[0].likelihood_ratio = 2.0; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "tiny_prior") == 0) {
        items[0].likelihood_ratio = 8.0; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "cap_near_one") == 0) {
        items[0].likelihood_ratio = 100.0; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else if (strcmp(name, "large_cap") == 0) {
        items[0].likelihood_ratio = 50.0; items[0].weight = 1.0; items[0].group_id = 0;
        CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, items, 1, &res));
    } else {
        fprintf(stderr, "golden.csv: unknown fusion case '%s' -- "
                "test_vectors.c needs a matching branch\n", name);
        exit(2);
    }

    CB_CHECK_NEAR(res.posterior, expected_post, 1e-12);
    CB_CHECK_NEAR(res.evidence_log_odds, expected_evid, 1e-12);
}

static void check_fault_tolerance_row(row_t *h, row_t *r)
{
    cb_ft_params_t p;
    int32_t n_min;
    double post_at_n, post_at_n_minus_1;
    int32_t expected_n = col_i(r, find_col(h, "n_min_sources"));
    double expected_post_at_n = col_d(r, find_col(h, "posterior_at_n_min"));
    const char *below_str = col_s(r, find_col(h, "posterior_at_n_min_minus_1"));

    CB_CHECK_OK(cb_ft_params_init(&p));
    p.lr_cap = col_d(r, find_col(h, "lr_cap"));
    p.prior = col_d(r, find_col(h, "prior"));
    p.threshold = col_d(r, find_col(h, "threshold"));
    p.max_weight = col_d(r, find_col(h, "max_weight"));
    p.odds_multiplier = col_d(r, find_col(h, "odds_multiplier"));

    CB_CHECK_OK(cb_ft_min_sources(&p, &n_min));
    CB_CHECK_EQ_INT(n_min, expected_n);

    CB_CHECK_OK(cb_ft_max_posterior(&p, n_min, &post_at_n));
    CB_CHECK_NEAR(post_at_n, expected_post_at_n, 1e-12);

    if (below_str[0] != '\0' && n_min > 0) {
        double expected_below = atof(below_str);
        CB_CHECK_OK(cb_ft_max_posterior(&p, n_min - 1, &post_at_n_minus_1));
        CB_CHECK_NEAR(post_at_n_minus_1, expected_below, 1e-12);
    }
}

static void check_decision_row(row_t *h, row_t *r)
{
    cb_cost_table_t t;
    cb_decision_result_t res;
    double posterior = col_d(r, find_col(h, "posterior"));
    double expected_ec_act = col_d(r, find_col(h, "expected_cost_act"));
    double expected_ec_hold = col_d(r, find_col(h, "expected_cost_hold"));
    double expected_thresh = col_d(r, find_col(h, "indifference_threshold"));
    const char *expected_action = col_s(r, find_col(h, "action"));

    t.cost_act_when_false = col_d(r, find_col(h, "cost_act_when_false"));
    t.cost_hold_when_true = col_d(r, find_col(h, "cost_hold_when_true"));
    t.cost_act_when_true = col_d(r, find_col(h, "cost_act_when_true"));
    t.cost_hold_when_false = col_d(r, find_col(h, "cost_hold_when_false"));

    CB_CHECK_OK(cb_decision_expected_cost(&t, posterior, &res));
    CB_CHECK_NEAR(res.expected_cost_act, expected_ec_act, 1e-12);
    CB_CHECK_NEAR(res.expected_cost_hold, expected_ec_hold, 1e-12);
    CB_CHECK_NEAR(res.indifference_threshold, expected_thresh, 1e-12);
    CB_CHECK_EQ_INT(res.action,
        (strcmp(expected_action, "ACT") == 0) ? CB_ACTION_ACT : CB_ACTION_HOLD);
}

static void check_calibration_row(row_t *h, row_t *r)
{
    cb_calibration_bounds_t b;
    cb_calibration_result_t res;

    b.lo = col_d(r, find_col(h, "lo"));
    b.hi = col_d(r, find_col(h, "hi"));
    b.max_step = col_d(r, find_col(h, "max_step"));
    b.rate = col_d(r, find_col(h, "rate"));

    CB_CHECK_OK(cb_calibration_step(&b, col_d(r, find_col(h, "current")),
                                    col_d(r, find_col(h, "gradient")), &res));
    CB_CHECK_NEAR(res.value, col_d(r, find_col(h, "value")), 1e-12);
    CB_CHECK_NEAR(res.raw_step, col_d(r, find_col(h, "raw_step")), 1e-12);
    CB_CHECK_NEAR(res.applied_step, col_d(r, find_col(h, "applied_step")), 1e-12);
    CB_CHECK_EQ_INT(res.step_clipped, col_i(r, find_col(h, "step_clipped")));
    CB_CHECK_EQ_INT(res.bound_clipped, col_i(r, find_col(h, "bound_clipped")));
}

static void check_graph_row(row_t *h, row_t *r)
{
    /* Reconstruct a chain graph 0 -> 1 -> 2 -> ... matching the vectors' raw multipliers. */
    cb_graph_config_t cfg;
    cb_graph_t g;
    double expected_composed = col_d(r, find_col(h, "composed_multiplier"));
    const char *name = col_s(r, find_col(h, "name"));
    double m;
    int32_t path[8];
    int i, n;
    double edges[8];

    CB_CHECK_OK(cb_graph_config_init(&cfg));
    cfg.m_min = col_d(r, find_col(h, "m_min"));
    cfg.m_max = col_d(r, find_col(h, "m_max"));
    cfg.n_nodes = 8;
    CB_CHECK_OK(cb_graph_init(&g, &cfg));

    if (strcmp(name, "single_lowers_edge") == 0) { n = 1; edges[0] = 0.5; }
    else if (strcmp(name, "seven_lowers_chain") == 0) {
        n = 7; for (i = 0; i < 7; ++i) { edges[i] = 0.5; }
    } else if (strcmp(name, "seven_raises_chain") == 0) {
        n = 7; for (i = 0; i < 7; ++i) { edges[i] = 3.0; }
    } else if (strcmp(name, "mixed_chain") == 0) {
        double vals[7] = {0.7, 2.0, 0.7, 2.0, 0.7, 2.0, 0.7};
        n = 7; for (i = 0; i < 7; ++i) { edges[i] = vals[i]; }
    } else if (strcmp(name, "flood_accident_jam") == 0) {
        n = 2; edges[0] = 1.5; edges[1] = 3.0;
    } else if (strcmp(name, "flood_fire_jam") == 0) {
        n = 2; edges[0] = 0.5; edges[1] = 1.5;
    } else {
        fprintf(stderr, "golden.csv: unknown graph case '%s'\n", name);
        exit(2);
    }

    for (i = 0; i < n; ++i) {
        cb_edge_kind_t kind = (edges[i] >= 1.0) ? CB_EDGE_RAISES : CB_EDGE_LOWERS;
        CB_CHECK_OK(cb_graph_set_edge(&g, i, i + 1, kind, edges[i]));
    }
    for (i = 0; i <= n; ++i) { path[i] = i; }
    CB_CHECK_OK(cb_graph_path_multiplier(&g, path, (size_t)(n + 1), &m));
    CB_CHECK_NEAR(m, expected_composed, 1e-12);
}

static void check_cache_row(row_t *h, row_t *r)
{
    cb_cache_key_t k;
    uint64_t expected_hash = col_u64(r, find_col(h, "hash"));

    CB_CHECK_OK(cb_cache_key_make(col_i(r, find_col(h, "category_id")),
                                  col_i(r, find_col(h, "partition_id")),
                                  col_i(r, find_col(h, "bucket_id")),
                                  col_u64(r, find_col(h, "state_version")),
                                  &k));
    CB_CHECK_TRUE(k.hash == expected_hash);
}

typedef void (*row_checker_t)(row_t *header, row_t *row);


static void check_physics_gaussian_lrt_row(row_t *h, row_t *r)
{
    double lr;
    double expected = col_d(r, find_col(h, "result"));
    double observed = col_d(r, find_col(h, "observed"));
    double h1 = col_d(r, find_col(h, "expected_h1"));
    double h0 = col_d(r, find_col(h, "expected_h0"));
    double sigma = col_d(r, find_col(h, "sigma"));
    cb_status_t status = cb_physics_gaussian_likelihood_ratio(observed, h1, h0, sigma, &lr);

    CB_CHECK_OK(status);
    CB_CHECK_NEAR(lr, expected, 1e-12);
}

static void check_physics_flood_row(row_t *h, row_t *r)
{
    double rate;
    double expected = col_d(r, find_col(h, "result"));
    double area = col_d(r, find_col(h, "area_m2"));
    double inflow = col_d(r, find_col(h, "inflow_m3_per_s"));
    double coeff = col_d(r, find_col(h, "drainage_coefficient"));
    double exponent = col_d(r, find_col(h, "drainage_exponent"));
    double level = col_d(r, find_col(h, "water_level_m"));
    cb_status_t status = cb_physics_flood_rise_rate(area, inflow, coeff, exponent,
                                                     level, &rate);

    CB_CHECK_OK(status);
    CB_CHECK_NEAR(rate, expected, 1e-12);
}

static void check_physics_fire_hrr_row(row_t *h, row_t *r)
{
    double hrr;
    double expected = col_d(r, find_col(h, "result"));
    double alpha = col_d(r, find_col(h, "alpha_kw_per_s2"));
    double time_s = col_d(r, find_col(h, "time_s"));
    cb_status_t status = cb_physics_fire_heat_release_rate(alpha, time_s, &hrr);

    CB_CHECK_OK(status);
    CB_CHECK_NEAR(hrr, expected, 1e-9);
}

static void check_physics_plume_row(row_t *h, row_t *r)
{
    double conc;
    double expected = col_d(r, find_col(h, "result"));
    double q = col_d(r, find_col(h, "emission_rate"));
    double u = col_d(r, find_col(h, "wind_speed_m_s"));
    double sy = col_d(r, find_col(h, "sigma_y_m"));
    double sz = col_d(r, find_col(h, "sigma_z_m"));
    double y = col_d(r, find_col(h, "crosswind_offset_m"));
    double z = col_d(r, find_col(h, "receptor_height_m"));
    double hgt = col_d(r, find_col(h, "effective_source_height_m"));
    cb_status_t status = cb_physics_plume_concentration(q, u, sy, sz, y, z, hgt, &conc);

    CB_CHECK_OK(status);
    CB_CHECK_NEAR(conc, expected, 1e-12);
}

static void check_physics_traffic_row(row_t *h, row_t *r)
{
    double speed, flow;
    double expected_speed = col_d(r, find_col(h, "speed_result"));
    double expected_flow = col_d(r, find_col(h, "flow_result"));
    double v_free = col_d(r, find_col(h, "free_flow_speed"));
    double k_jam = col_d(r, find_col(h, "jam_density"));
    double k = col_d(r, find_col(h, "density"));
    cb_status_t status_a = cb_physics_traffic_greenshields_speed(v_free, k_jam, k, &speed);
    cb_status_t status_b = cb_physics_traffic_greenshields_flow(v_free, k_jam, k, &flow);

    CB_CHECK_OK(status_a);
    CB_CHECK_NEAR(speed, expected_speed, 1e-12);
    CB_CHECK_OK(status_b);
    CB_CHECK_NEAR(flow, expected_flow, 1e-12);
}

static row_checker_t checker_for_section(const char *section)
{
    if (strcmp(section, "fusion") == 0) { return check_fusion_row; }
    if (strcmp(section, "fault_tolerance") == 0) { return check_fault_tolerance_row; }
    if (strcmp(section, "decision") == 0) { return check_decision_row; }
    if (strcmp(section, "calibration") == 0) { return check_calibration_row; }
    if (strcmp(section, "graph") == 0) { return check_graph_row; }
    if (strcmp(section, "cache") == 0) { return check_cache_row; }
    if (strcmp(section, "physics_gaussian_lrt") == 0) { return check_physics_gaussian_lrt_row; }
    if (strcmp(section, "physics_flood") == 0) { return check_physics_flood_row; }
    if (strcmp(section, "physics_fire_hrr") == 0) { return check_physics_fire_hrr_row; }
    if (strcmp(section, "physics_plume") == 0) { return check_physics_plume_row; }
    if (strcmp(section, "physics_traffic") == 0) { return check_physics_traffic_row; }
    return NULL;
}

int main(void)
{
    FILE *f = fopen(CB_VECTORS_PATH, "r");
    char line[MAX_LINE];
    char header_line[MAX_LINE];
    row_t header;
    int have_header = 0;
    row_checker_t checker = NULL;
    int total_rows = 0;
    char section_name[128] = "";

    if (f == NULL) {
        fprintf(stderr, "cannot open golden vectors at %s\n", CB_VECTORS_PATH);
        return 2;
    }

    while (fgets(line, sizeof line, f) != NULL) {
        char buf[MAX_LINE];
        trim_newline(line);
        if (line[0] == '\0') { continue; }
        if (line[0] == '#' && strncmp(line, "#SECTION:", 9) == 0) {
            strncpy(section_name, line + 9, sizeof section_name - 1);
            section_name[sizeof section_name - 1] = '\0';
            checker = checker_for_section(section_name);
            have_header = 0;
            if (checker == NULL) {
                fprintf(stderr, "no checker registered for section '%s'\n", section_name);
                return 2;
            }
            continue;
        }
        if (line[0] == '#') { continue; } /* top-of-file comments */

        strncpy(buf, line, sizeof buf - 1);
        buf[sizeof buf - 1] = '\0';

        if (!have_header) {
            strncpy(header_line, buf, sizeof header_line - 1);
            header_line[sizeof header_line - 1] = '\0';
            header.n_fields = split_csv(header_line, header.fields, MAX_FIELDS);
            have_header = 1;
            continue;
        }

        {
            row_t row;
            row.n_fields = split_csv(buf, row.fields, MAX_FIELDS);
            if (row.n_fields == 0) { continue; }
            CB_CASE(section_name);
            checker(&header, &row);
            total_rows++;
        }
    }
    fclose(f);

    printf("test_vectors: checked %d golden rows across all sections\n", total_rows);
    CB_CHECK_TRUE(total_rows >= 60);
    return cb_t_report("test_vectors");
}
