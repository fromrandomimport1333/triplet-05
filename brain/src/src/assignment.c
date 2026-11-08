#include "citybrain/assignment.h"
#include "internal.h"

/* Hungarian method (Jonker-Volgenant shortest-augmenting-path form),
 * O(n^2 * m) for an n x m matrix with n <= m. Scratch comes from a bump
 * allocator over the caller's buffer; the library itself never allocates. */

#define CB_ALIGN_UP(x, a) (((x) + ((a) - 1u)) & ~((size_t)(a) - 1u))

typedef struct bump {
    unsigned char *base;
    size_t used;
    size_t capacity;
    int overflow;
} bump_t;

static void *bump_take(bump_t *b, size_t bytes, size_t align)
{
    size_t start = CB_ALIGN_UP(b->used, align);
    if (b->overflow || start > b->capacity || bytes > b->capacity - start) {
        b->overflow = 1;
        return NULL;
    }
    b->used = start + bytes;
    return (b->base == NULL) ? NULL : (void *)(b->base + start);
}

/* Lay out the working set, either measuring (base == NULL) or allocating. */
static size_t assign_layout(size_t n, size_t m, bump_t *b,
                            double **cost, unsigned char **forbid,
                            double **u, double **v, double **minv,
                            int32_t **p, int32_t **way, unsigned char **used,
                            int32_t **result)
{
    *cost   = (double *)bump_take(b, n * m * sizeof(double), sizeof(double));
    *forbid = (unsigned char *)bump_take(b, n * m, 1u);
    *u      = (double *)bump_take(b, (n + 1u) * sizeof(double), sizeof(double));
    *v      = (double *)bump_take(b, (m + 1u) * sizeof(double), sizeof(double));
    *minv   = (double *)bump_take(b, (m + 1u) * sizeof(double), sizeof(double));
    *p      = (int32_t *)bump_take(b, (m + 1u) * sizeof(int32_t), sizeof(int32_t));
    *way    = (int32_t *)bump_take(b, (m + 1u) * sizeof(int32_t), sizeof(int32_t));
    *used   = (unsigned char *)bump_take(b, (m + 1u), 1u);
    *result = (int32_t *)bump_take(b, n * sizeof(int32_t), sizeof(int32_t));
    return CB_ALIGN_UP(b->used, sizeof(double));
}

cb_status_t cb_assign_params_init(cb_assign_params_t *params)
{
    CB_REQUIRE(params != NULL, CB_ERR_NULL);
    params->matrix        = NULL;
    params->n_rows        = 0;
    params->n_cols        = 0;
    params->maximize      = 0;
    params->infeasible_at = HUGE_VAL;
    return CB_OK;
}

cb_status_t cb_assign_scratch_bytes(int32_t n_rows, int32_t n_cols, size_t *out_bytes)
{
    size_t n, m;
    bump_t b;
    double *c, *u, *v, *mv;
    unsigned char *f, *us;
    int32_t *p, *w, *r;

    CB_REQUIRE(out_bytes != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_rows >= 1 && n_cols >= 1, CB_ERR_DOMAIN);
    CB_REQUIRE(n_rows <= CB_ASSIGN_MAX_DIM && n_cols <= CB_ASSIGN_MAX_DIM,
               CB_ERR_CAPACITY);

    n = (size_t)((n_rows < n_cols) ? n_rows : n_cols);
    m = (size_t)((n_rows < n_cols) ? n_cols : n_rows);

    b.base = NULL; b.used = 0; b.capacity = (size_t)-1; b.overflow = 0;
    *out_bytes = assign_layout(n, m, &b, &c, &f, &u, &v, &mv, &p, &w, &us, &r);
    return CB_OK;
}

/* The core solver. cost is n x m row-major with n <= m, every entry finite.
 * result[i] receives the column matched to row i; every row is matched. */
static void hungarian_core(const double *cost, size_t n, size_t m,
                           double *u, double *v, double *minv,
                           int32_t *p, int32_t *way, unsigned char *used,
                           int32_t *result)
{
    size_t i, j;

    for (j = 0; j <= m; ++j) {
        v[j] = 0.0; p[j] = 0; way[j] = 0;
    }
    for (i = 0; i <= n; ++i) {
        u[i] = 0.0;
    }

    for (i = 1; i <= n; ++i) {
        size_t j0 = 0;
        p[0] = (int32_t)i;
        for (j = 0; j <= m; ++j) {
            minv[j] = HUGE_VAL;
            used[j] = 0u;
        }
        do {
            size_t i0, j1 = 0;
            double delta = HUGE_VAL;

            used[j0] = 1u;
            i0 = (size_t)p[j0];
            /* Strict comparison: lowest column index wins ties, so the
             * result is reproducible. */
            for (j = 1; j <= m; ++j) {
                double cur;
                if (used[j]) { continue; }
                cur = cost[(i0 - 1u) * m + (j - 1u)] - u[i0] - v[j];
                if (cur < minv[j]) {
                    minv[j] = cur;
                    way[j] = (int32_t)j0;
                }
                if (minv[j] < delta) {
                    delta = minv[j];
                    j1 = j;
                }
            }
            for (j = 0; j <= m; ++j) {
                if (used[j]) {
                    u[(size_t)p[j]] += delta;
                    v[j] -= delta;
                } else {
                    minv[j] -= delta;
                }
            }
            j0 = j1;
        } while (p[j0] != 0);

        do {
            size_t j1 = (size_t)way[j0];
            p[j0] = p[j1];
            j0 = j1;
        } while (j0 != 0);
    }

    for (i = 0; i < n; ++i) {
        result[i] = -1;
    }
    for (j = 1; j <= m; ++j) {
        if (p[j] != 0) {
            result[(size_t)p[j] - 1u] = (int32_t)(j - 1u);
        }
    }
}

cb_status_t cb_assign_solve(const cb_assign_params_t *params,
                            int32_t *out_assignment, double *out_total,
                            void *scratch, size_t scratch_bytes)
{
    size_t n, m, i, j, count;
    int transposed;
    bump_t b;
    double *cost, *u, *v, *minv;
    unsigned char *forbid, *used;
    int32_t *p, *way, *result;
    double big, max_allowed = 0.0, min_allowed = 0.0;
    int any_allowed = 0;
    double total = 0.0;
    size_t needed = 0;

    CB_REQUIRE(params != NULL && out_assignment != NULL, CB_ERR_NULL);
    CB_REQUIRE(params->matrix != NULL, CB_ERR_NULL);
    CB_REQUIRE(params->n_rows >= 1 && params->n_cols >= 1, CB_ERR_DOMAIN);
    CB_REQUIRE(params->n_rows <= CB_ASSIGN_MAX_DIM &&
               params->n_cols <= CB_ASSIGN_MAX_DIM, CB_ERR_CAPACITY);
    CB_REQUIRE(params->maximize == 0 || params->maximize == 1, CB_ERR_DOMAIN);
    CB_REQUIRE(!isnan(params->infeasible_at), CB_ERR_NOT_FINITE);
    CB_REQUIRE(scratch != NULL, CB_ERR_NULL);
    /* The scratch buffer holds doubles; an unaligned buffer would be
     * undefined behaviour rather than a wrong answer. */
    CB_REQUIRE((((uintptr_t)scratch) % sizeof(double)) == 0u, CB_ERR_DOMAIN);

    count = (size_t)params->n_rows * (size_t)params->n_cols;
    CB_CHECK(cb_finite_array(params->matrix, count));

    CB_CHECK(cb_assign_scratch_bytes(params->n_rows, params->n_cols, &needed));
    CB_REQUIRE(scratch_bytes >= needed, CB_ERR_CAPACITY);

    transposed = (params->n_rows > params->n_cols) ? 1 : 0;
    n = (size_t)(transposed ? params->n_cols : params->n_rows);
    m = (size_t)(transposed ? params->n_rows : params->n_cols);

    b.base = (unsigned char *)scratch;
    b.used = 0;
    b.capacity = scratch_bytes;
    b.overflow = 0;
    (void)assign_layout(n, m, &b, &cost, &forbid, &u, &v, &minv, &p, &way, &used,
                        &result);
    CB_REQUIRE(b.overflow == 0, CB_ERR_CAPACITY);

    /* Normalise orientation and sign, and mark forbidden pairs. */
    for (i = 0; i < n; ++i) {
        for (j = 0; j < m; ++j) {
            size_t src = transposed ? (j * (size_t)params->n_cols + i)
                                    : (i * (size_t)params->n_cols + j);
            double raw = params->matrix[src];
            double value = params->maximize ? -raw : raw;
            int bad = params->maximize ? (raw <= params->infeasible_at)
                                       : (raw >= params->infeasible_at);
            forbid[i * m + j] = bad ? 1u : 0u;
            cost[i * m + j] = value;
            if (!bad) {
                if (!any_allowed) {
                    max_allowed = value;
                    min_allowed = value;
                    any_allowed = 1;
                } else {
                    if (value > max_allowed) { max_allowed = value; }
                    if (value < min_allowed) { min_allowed = value; }
                }
            }
        }
    }

    /* Replace forbidden entries with a penalty large enough that any
     * complete assignment avoiding them beats one that doesn't. */
    {
        double a = (max_allowed < 0.0) ? -max_allowed : max_allowed;
        double c = (min_allowed < 0.0) ? -min_allowed : min_allowed;
        big = (double)n * (a + c) + 1.0;
        if (!cb_finite(big)) {
            return CB_ERR_CAPACITY; /* costs span too much of the double range */
        }
    }
    for (i = 0; i < n * m; ++i) {
        if (forbid[i]) { cost[i] = big; }
    }

    hungarian_core(cost, n, m, u, v, minv, p, way, used, result);

    for (i = 0; i < (size_t)params->n_rows; ++i) {
        out_assignment[i] = -1;
    }
    for (i = 0; i < n; ++i) {
        int32_t col = result[i];
        size_t orig_row, orig_col;
        if (col < 0) { continue; }
        orig_row = transposed ? (size_t)col : i;
        orig_col = transposed ? i : (size_t)col;
        if (forbid[i * m + (size_t)col]) {
            for (j = 0; j < (size_t)params->n_rows; ++j) {
                out_assignment[j] = -1;
            }
            return CB_ERR_NO_SOLUTION;
        }
        out_assignment[orig_row] = (int32_t)orig_col;
        total += params->matrix[orig_row * (size_t)params->n_cols + orig_col];
    }

    if (out_total != NULL) { *out_total = total; }
    return CB_OK;
}
