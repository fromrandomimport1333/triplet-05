#include "citybrain/calibration.h"
#include "internal.h"

cb_status_t cb_calibration_bounds_init(cb_calibration_bounds_t *bounds)
{
    CB_REQUIRE(bounds != NULL, CB_ERR_NULL);
    bounds->lo       = 0.0;
    bounds->hi       = 1.0;
    bounds->max_step = 0.05;
    bounds->rate     = 0.1;
    return CB_OK;
}

static cb_status_t bounds_validate(const cb_calibration_bounds_t *b)
{
    CB_REQUIRE(b != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(b->lo) && cb_finite(b->hi) &&
               cb_finite(b->max_step) && cb_finite(b->rate), CB_ERR_NOT_FINITE);
    CB_REQUIRE(b->hi >= b->lo, CB_ERR_DOMAIN);
    CB_REQUIRE(b->max_step >= 0.0, CB_ERR_DOMAIN);
    return CB_OK;
}

cb_status_t cb_calibration_step(const cb_calibration_bounds_t *bounds,
                                double current, double gradient,
                                cb_calibration_result_t *out)
{
    double raw, stepped, candidate, final_value;

    CB_REQUIRE(out != NULL, CB_ERR_NULL);
    CB_CHECK(bounds_validate(bounds));
    CB_REQUIRE(cb_finite(current) && cb_finite(gradient), CB_ERR_NOT_FINITE);
    /* current outside the bounds means the caller's state already escaped them. */
    CB_REQUIRE(current >= bounds->lo && current <= bounds->hi, CB_ERR_DOMAIN);

    raw = bounds->rate * gradient;
    CB_REQUIRE(cb_finite(raw), CB_ERR_NOT_FINITE);

    stepped = cb_clamp(raw, -bounds->max_step, bounds->max_step);
    candidate = current + stepped;
    final_value = cb_clamp(candidate, bounds->lo, bounds->hi);

    out->value         = final_value;
    out->raw_step      = raw;
    out->applied_step  = final_value - current;
    out->step_clipped  = (raw != stepped) ? 1 : 0;
    out->bound_clipped = (candidate != final_value) ? 1 : 0;
    return CB_OK;
}

cb_status_t cb_calibration_min_steps(const cb_calibration_bounds_t *bounds,
                                     double from, double to, int32_t *out_steps)
{
    double distance, needed;

    CB_REQUIRE(out_steps != NULL, CB_ERR_NULL);
    CB_CHECK(bounds_validate(bounds));
    CB_REQUIRE(cb_finite(from) && cb_finite(to), CB_ERR_NOT_FINITE);

    distance = (to > from) ? (to - from) : (from - to);
    if (distance == 0.0) {
        *out_steps = 0;
        return CB_OK;
    }
    CB_REQUIRE(bounds->max_step > 0.0, CB_ERR_DOMAIN); /* the move is impossible */

    needed = ceil(distance / bounds->max_step);
    if (!cb_finite(needed) || needed > 2147483647.0) {
        return CB_ERR_CAPACITY;
    }
    *out_steps = (int32_t)needed;
    return CB_OK;
}
