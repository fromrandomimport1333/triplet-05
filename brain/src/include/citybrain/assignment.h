/*
 * Optimal bipartite assignment (Hungarian algorithm, O(n^2 m)). Picks the
 * minimum-cost (or maximum-benefit) one-to-one matching between rows and
 * columns. A greedy nearest-match isn't optimal and gives a different
 * answer depending on input order, which matters when a decision needs to
 * be logged and reproduced -- the Hungarian method has an exact solution,
 * so there's no reason to approximate it.
 */
#ifndef CITYBRAIN_ASSIGNMENT_H
#define CITYBRAIN_ASSIGNMENT_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_ASSIGN_MAX_DIM ((int32_t)2048)

typedef struct cb_assign_params {
    const double *matrix;   /* row-major, n_rows * n_cols */
    int32_t n_rows;         /* in [1, CB_ASSIGN_MAX_DIM] */
    int32_t n_cols;         /* in [1, CB_ASSIGN_MAX_DIM] */
    int32_t maximize;       /* 0: matrix holds costs, 1: benefits */
    double  infeasible_at;  /* entries at or past this are forbidden pairs */
} cb_assign_params_t;

/* Default: NULL matrix, 0 dims, maximize=0, infeasible_at=+infinity. */
CB_API cb_status_t cb_assign_params_init(cb_assign_params_t *params);

/* Bytes of scratch required for the given dimensions. */
CB_API cb_status_t cb_assign_scratch_bytes(int32_t n_rows, int32_t n_cols,
                                           size_t *out_bytes);

/*
 * Solve. out_assignment has n_rows entries; -1 means that row is
 * unassigned (only possible when n_rows > n_cols). Ties are broken by
 * lowest column index, so the result is reproducible.
 *
 * Errors: CB_ERR_CAPACITY for a dimension too large or scratch too small;
 * CB_ERR_NOT_FINITE for a bad matrix entry; CB_ERR_NO_SOLUTION if every
 * complete assignment would need a forbidden pair.
 */
CB_API cb_status_t cb_assign_solve(const cb_assign_params_t *params,
                                   int32_t *out_assignment,
                                   double *out_total,
                                   void *scratch,
                                   size_t scratch_bytes);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_ASSIGNMENT_H */
