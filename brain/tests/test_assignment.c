/* Hungarian assignment: optimality, shapes, forbidden pairs, determinism. */
#include "cb_test.h"

#define SCRATCH_BYTES (1u << 20)
static unsigned char g_scratch[SCRATCH_BYTES];

/* Independent brute force over all permutations, for small matrices. */
static void brute_force_rec(const double *m, int n, int cols, int row,
                            int *used, double acc, double *best)
{
    int j;
    if (row == n) {
        if (acc < *best) { *best = acc; }
        return;
    }
    for (j = 0; j < cols; ++j) {
        if (used[j]) { continue; }
        used[j] = 1;
        brute_force_rec(m, n, cols, row + 1, used, acc + m[row * cols + j], best);
        used[j] = 0;
    }
}

static double brute_force_min(const double *m, int n, int cols)
{
    int used[8];
    double best = HUGE_VAL;
    int i;
    for (i = 0; i < 8; ++i) { used[i] = 0; }
    if (n > 8 || cols > 8 || n > cols) { return best; }
    brute_force_rec(m, n, cols, 0, used, 0.0, &best);
    return best;
}

static void test_small_known(void)
{
    cb_assign_params_t p;
    int32_t a[4];
    double total;
    /* Classic 3x3 instance; the optimum is 5 via (0,1),(1,0),(2,2). */
    static const double m[9] = {
        4.0, 1.0, 3.0,
        2.0, 0.0, 5.0,
        3.0, 2.0, 2.0
    };

    CB_CASE("3x3 textbook instance reaches the known optimum");
    CB_CHECK_OK(cb_assign_params_init(&p));
    p.matrix = m; p.n_rows = 3; p.n_cols = 3;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    CB_CHECK_NEAR(total, 5.0, 1e-12);
    CB_CHECK_EQ_INT(a[0], 1);
    CB_CHECK_EQ_INT(a[1], 0);
    CB_CHECK_EQ_INT(a[2], 2);

    CB_CASE("1x1 is the degenerate case and still works");
    {
        double one = 7.5;
        p.matrix = &one; p.n_rows = 1; p.n_cols = 1;
        CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
        CB_CHECK_EQ_INT(a[0], 0);
        CB_CHECK_NEAR(total, 7.5, 0.0);
    }
}

static void test_rectangular(void)
{
    cb_assign_params_t p;
    int32_t a[5];
    double total;
    /* 2 incidents, 4 units available. */
    static const double wide[8] = {
        9.0, 1.0, 8.0, 7.0,
        6.0, 2.0, 3.0, 9.0
    };
    /* 4 incidents, 2 units available: two incidents go unserved. */
    static const double tall[8] = {
        9.0, 6.0,
        1.0, 2.0,
        8.0, 3.0,
        7.0, 9.0
    };
    int i, assigned;

    CB_CASE("more units than incidents: every incident is served");
    CB_CHECK_OK(cb_assign_params_init(&p));
    p.matrix = wide; p.n_rows = 2; p.n_cols = 4;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    CB_CHECK_EQ_INT(a[0], 1);
    CB_CHECK_EQ_INT(a[1], 2);
    CB_CHECK_NEAR(total, 4.0, 1e-12);

    CB_CASE("more incidents than units: the surplus is marked unassigned");
    p.matrix = tall; p.n_rows = 4; p.n_cols = 2;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    assigned = 0;
    for (i = 0; i < 4; ++i) {
        if (a[i] >= 0) { assigned++; }
    }
    CB_CHECK_EQ_INT(assigned, 2);
    CB_CHECK_NEAR(total, 4.0, 1e-12);
    CB_CHECK_EQ_INT(a[1], 0);
    CB_CHECK_EQ_INT(a[2], 1);
    CB_CHECK_EQ_INT(a[0], -1);
    CB_CHECK_EQ_INT(a[3], -1);
}

static void test_maximize(void)
{
    cb_assign_params_t p;
    int32_t a[3];
    double total;
    static const double benefit[9] = {
        4.0, 1.0, 3.0,
        2.0, 0.0, 5.0,
        3.0, 2.0, 2.0
    };

    CB_CASE("maximising a benefit matrix picks the best total, not the cheapest");
    CB_CHECK_OK(cb_assign_params_init(&p));
    p.matrix = benefit; p.n_rows = 3; p.n_cols = 3;
    p.maximize = 1;
    p.infeasible_at = -HUGE_VAL;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    /* best is (0,0)=4 + (1,2)=5 + (2,1)=2 = 11 */
    CB_CHECK_NEAR(total, 11.0, 1e-12);
    CB_CHECK_EQ_INT(a[0], 0);
    CB_CHECK_EQ_INT(a[1], 2);
    CB_CHECK_EQ_INT(a[2], 1);
}

static void test_forbidden(void)
{
    cb_assign_params_t p;
    int32_t a[3];
    double total;
    static double m[9] = {
        1.0,   1000.0, 1000.0,
        1000.0, 1.0,   1000.0,
        1000.0, 1000.0, 1.0
    };
    static double blocked[4] = {
        1000.0, 1000.0,
        1.0,    2.0
    };

    CB_CASE("forbidden pairs are avoided when an alternative exists");
    CB_CHECK_OK(cb_assign_params_init(&p));
    p.matrix = m; p.n_rows = 3; p.n_cols = 3;
    p.infeasible_at = 999.0;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    CB_CHECK_NEAR(total, 3.0, 1e-12);

    CB_CASE("a row with no feasible column reports NO_SOLUTION, not a wrong answer");
    p.matrix = blocked; p.n_rows = 2; p.n_cols = 2;
    p.infeasible_at = 999.0;
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NO_SOLUTION);
    CB_CHECK_EQ_INT(a[0], -1);
    CB_CHECK_EQ_INT(a[1], -1);

    CB_CASE("disabling the feature lets the expensive pair be used");
    /* Without the forbidden marking the optimum is row0->col1 (1000) plus
     * row1->col0 (1) = 1001, which is cheaper than the diagonal's 1002. */
    p.infeasible_at = HUGE_VAL;
    CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
    CB_CHECK_NEAR(total, 1001.0, 1e-9);
    CB_CHECK_EQ_INT(a[0], 1);
    CB_CHECK_EQ_INT(a[1], 0);
}

static void test_against_brute_force(void)
{
    cb_assign_params_t p;
    int32_t a[6];
    double total, expected;
    uint64_t seed = 42u;
    int trial, i, n;
    double m[36];
    int mismatches = 0;

    CB_CASE("200 random instances match an independent brute-force optimum");
    for (trial = 0; trial < 200; ++trial) {
        n = 2 + (int)(trial % 5); /* 2..6 */
        for (i = 0; i < n * n; ++i) {
            seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
            m[i] = (double)((seed >> 33) % 1000u) / 10.0;
        }
        CB_CHECK_OK(cb_assign_params_init(&p));
        p.matrix = m; p.n_rows = n; p.n_cols = n;
        CB_CHECK_OK(cb_assign_solve(&p, a, &total, g_scratch, SCRATCH_BYTES));
        expected = brute_force_min(m, n, n);
        if (fabs(total - expected) > 1e-9) { mismatches++; }
        CB_CHECK_NEAR(total, expected, 1e-9);
        /* the returned assignment must actually be a permutation */
        {
            int seen[8];
            double recomputed = 0.0;
            for (i = 0; i < n; ++i) { seen[i] = 0; }
            for (i = 0; i < n; ++i) {
                CB_CHECK_TRUE(a[i] >= 0 && a[i] < n);
                CB_CHECK_TRUE(seen[a[i]] == 0);
                seen[a[i]] = 1;
                recomputed += m[i * n + a[i]];
            }
            CB_CHECK_NEAR(recomputed, total, 1e-12);
        }
    }
    CB_CHECK_EQ_INT(mismatches, 0);
    printf("  200 random instances, 0 mismatches against brute force\n");
}

static void test_determinism_and_errors(void)
{
    cb_assign_params_t p;
    int32_t a[3], b[3];
    double t1, t2;
    size_t need;
    /* Every assignment costs the same; the tie-break must still be fixed. */
    static const double flat[9] = {
        1.0, 1.0, 1.0,
        1.0, 1.0, 1.0,
        1.0, 1.0, 1.0
    };
    static const double bad[4] = { 1.0, 2.0, 3.0, 4.0 };
    static double nanmat[4] = { 1.0, 2.0, 3.0, 4.0 };

    CB_CASE("a fully tied matrix yields the identity, every time");
    CB_CHECK_OK(cb_assign_params_init(&p));
    p.matrix = flat; p.n_rows = 3; p.n_cols = 3;
    CB_CHECK_OK(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES));
    CB_CHECK_OK(cb_assign_solve(&p, b, &t2, g_scratch, SCRATCH_BYTES));
    CB_CHECK_EQ_INT(a[0], 0);
    CB_CHECK_EQ_INT(a[1], 1);
    CB_CHECK_EQ_INT(a[2], 2);
    CB_CHECK_EQ_INT(a[0], b[0]);
    CB_CHECK_EQ_INT(a[1], b[1]);
    CB_CHECK_EQ_INT(a[2], b[2]);
    CB_CHECK_NEAR(t1, t2, 0.0);

    CB_CASE("scratch sizing is exact and a short buffer is a capacity error");
    CB_CHECK_OK(cb_assign_scratch_bytes(3, 3, &need));
    CB_CHECK_TRUE(need > 0 && need < SCRATCH_BYTES);
    CB_CHECK_OK(cb_assign_solve(&p, a, &t1, g_scratch, need));
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch, need - 1),
                    CB_ERR_CAPACITY);

    CB_CASE("argument domains");
    CB_CHECK_STATUS(cb_assign_scratch_bytes(0, 3, &need), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_assign_scratch_bytes(CB_ASSIGN_MAX_DIM + 1, 3, &need),
                    CB_ERR_CAPACITY);
    CB_CHECK_STATUS(cb_assign_scratch_bytes(3, 3, NULL), CB_ERR_NULL);

    p.matrix = bad; p.n_rows = 0; p.n_cols = 2;
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_DOMAIN);
    p.n_rows = 2;
    p.matrix = NULL;
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NULL);
    p.matrix = bad;
    CB_CHECK_STATUS(cb_assign_solve(&p, NULL, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NULL);
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, NULL, SCRATCH_BYTES), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_assign_solve(NULL, a, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NULL);

    CB_CASE("non-finite matrix entries are rejected");
    nanmat[2] = (double)NAN;
    p.matrix = nanmat; p.n_rows = 2; p.n_cols = 2;
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NOT_FINITE);
    nanmat[2] = HUGE_VAL;
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES),
                    CB_ERR_NOT_FINITE);
    nanmat[2] = 3.0;

    CB_CASE("an unaligned scratch pointer is refused rather than risked");
    CB_CHECK_STATUS(cb_assign_solve(&p, a, &t1, g_scratch + 1, SCRATCH_BYTES - 1),
                    CB_ERR_DOMAIN);

    CB_CASE("out_total may be NULL");
    CB_CHECK_OK(cb_assign_solve(&p, a, NULL, g_scratch, SCRATCH_BYTES));

    CB_CASE("negative costs are allowed");
    {
        static const double neg[4] = { -5.0, -1.0, -2.0, -9.0 };
        p.matrix = neg; p.n_rows = 2; p.n_cols = 2;
        CB_CHECK_OK(cb_assign_solve(&p, a, &t1, g_scratch, SCRATCH_BYTES));
        CB_CHECK_NEAR(t1, -14.0, 1e-12);
    }
}

int main(void)
{
    test_small_known();
    test_rectangular();
    test_maximize();
    test_forbidden();
    test_against_brute_force();
    test_determinism_and_errors();
    return cb_t_report("test_assignment");
}
