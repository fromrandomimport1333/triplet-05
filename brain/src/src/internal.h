/*
 * Internal helpers shared across the implementation. Not installed, not part
 * of the ABI. Everything here is static or a macro, so it adds no symbols and
 * no mutable state.
 */
#ifndef CITYBRAIN_INTERNAL_H
#define CITYBRAIN_INTERNAL_H

#include "citybrain/common.h"
#include <math.h>

/* Initialisation guards. Chosen to be unlikely in uninitialised memory. */
#define CB_MAGIC_DETECTOR 0x43424454u /* CBDT */
#define CB_MAGIC_GRAPH    0x43424752u /* CBGR */

#define CB_REQUIRE(cond, code) do { if (!(cond)) { return (code); } } while (0)
#define CB_CHECK(expr) do { cb_status_t cb_s_ = (expr); \
                            if (cb_s_ != CB_OK) { return cb_s_; } } while (0)

static inline int cb_finite(double x)
{
    return isfinite(x) ? 1 : 0;
}

/* Finite check for an array; returns the status directly. */
static inline cb_status_t cb_finite_array(const double *v, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        if (!cb_finite(v[i])) {
            return CB_ERR_NOT_FINITE;
        }
    }
    return CB_OK;
}

static inline double cb_clamp(double x, double lo, double hi)
{
    if (x < lo) { return lo; }
    if (x > hi) { return hi; }
    return x;
}

#endif /* CITYBRAIN_INTERNAL_H */
