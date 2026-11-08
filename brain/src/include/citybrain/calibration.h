/*
 * A bounded parameter update: theta_next = clip(theta + clip(rate*gradient,
 * -max_step, max_step), lo, hi). Every update moves by at most max_step and
 * the result never leaves [lo, hi], however extreme the gradient is. That
 * means walking a parameter any meaningful distance takes a known minimum
 * number of accepted updates, which is what stops a slow, repeated nudge
 * from poisoning a learned threshold unnoticed.
 */
#ifndef CITYBRAIN_CALIBRATION_H
#define CITYBRAIN_CALIBRATION_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cb_calibration_bounds {
    double lo;
    double hi;       /* >= lo */
    double max_step; /* >= 0 */
    double rate;
} cb_calibration_bounds_t;

typedef struct cb_calibration_result {
    double  value;
    double  raw_step;      /* rate * gradient, before clipping */
    double  applied_step;  /* actual change after both clips */
    int32_t step_clipped;  /* 1 if max_step bound was hit */
    int32_t bound_clipped; /* 1 if the [lo, hi] bound was hit */
} cb_calibration_result_t;

/* Default: lo=0, hi=1, max_step=0.05, rate=0.1. */
CB_API cb_status_t cb_calibration_bounds_init(cb_calibration_bounds_t *bounds);

/* One bounded update. current must already be inside [lo, hi] --
 * CB_ERR_DOMAIN otherwise, since that means the caller's stored state has
 * already escaped the bounds. */
CB_API cb_status_t cb_calibration_step(const cb_calibration_bounds_t *bounds,
                                       double current,
                                       double gradient,
                                       cb_calibration_result_t *out);

/* Minimum accepted updates to move from one value to another, ignoring the
 * gradient: ceil(|to-from| / max_step). CB_ERR_DOMAIN if max_step==0 and
 * the values differ. */
CB_API cb_status_t cb_calibration_min_steps(const cb_calibration_bounds_t *bounds,
                                            double from,
                                            double to,
                                            int32_t *out_steps);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_CALIBRATION_H */
