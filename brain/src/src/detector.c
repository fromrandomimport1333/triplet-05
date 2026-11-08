#include "citybrain/detector.h"
#include "internal.h"

cb_status_t cb_detector_config_init(cb_detector_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    /* k=0.5, h=5 is the standard CUSUM design point for a one-sigma shift
     * (Basseville & Nikiforov 1993); see cb_detector_arl0/arl1. */
    cfg->ewma_alpha            = 0.10;
    cfg->var_alpha             = 0.05;
    cfg->min_sigma             = 1e-6;
    cfg->cusum_slack           = 0.5;
    cfg->cusum_threshold       = 5.0;
    cfg->baseline_hold_enabled = 1;
    cfg->baseline_hold_frac    = 0.5;
    cfg->warmup_samples        = 20u;
    return CB_OK;
}

cb_status_t cb_detector_config_validate(const cb_detector_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(cfg->ewma_alpha) && cb_finite(cfg->var_alpha) &&
               cb_finite(cfg->min_sigma) && cb_finite(cfg->cusum_slack) &&
               cb_finite(cfg->cusum_threshold) && cb_finite(cfg->baseline_hold_frac),
               CB_ERR_NOT_FINITE);
    CB_REQUIRE(cfg->ewma_alpha > 0.0 && cfg->ewma_alpha <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->var_alpha > 0.0 && cfg->var_alpha <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->min_sigma > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->cusum_slack >= 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->cusum_threshold > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->baseline_hold_enabled == 0 || cfg->baseline_hold_enabled == 1,
               CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->baseline_hold_frac >= 0.0 && cfg->baseline_hold_frac <= 1.0,
               CB_ERR_DOMAIN);
    return CB_OK;
}

cb_status_t cb_detector_init(cb_detector_state_t *state,
                             const cb_detector_config_t *cfg,
                             double first_value,
                             double initial_sigma)
{
    CB_REQUIRE(state != NULL, CB_ERR_NULL);
    CB_CHECK(cb_detector_config_validate(cfg));
    CB_REQUIRE(cb_finite(first_value) && cb_finite(initial_sigma), CB_ERR_NOT_FINITE);
    CB_REQUIRE(initial_sigma > 0.0, CB_ERR_DOMAIN);

    state->ewma       = first_value;
    state->ewma_var   = initial_sigma * initial_sigma;
    state->cusum_high = 0.0;
    state->cusum_low  = 0.0;
    state->n_seen     = 0u;
    state->magic      = CB_MAGIC_DETECTOR;
    return CB_OK;
}

cb_status_t cb_detector_update(cb_detector_state_t *state,
                               const cb_detector_config_t *cfg,
                               double value,
                               cb_detector_output_t *out)
{
    double sigma, residual, z;
    uint32_t flags = CB_DET_NONE;
    int in_warmup;
    int hold_baseline = 0;

    CB_REQUIRE(state != NULL, CB_ERR_NULL);
    CB_CHECK(cb_detector_config_validate(cfg));
    CB_REQUIRE(state->magic == CB_MAGIC_DETECTOR, CB_ERR_STATE);
    CB_REQUIRE(cb_finite(value), CB_ERR_NOT_FINITE);

    /* 1. residual against the current baseline and scale */
    sigma = sqrt(state->ewma_var);
    if (!(sigma > cfg->min_sigma)) {
        sigma = cfg->min_sigma;
    }
    residual = value - state->ewma;
    z = residual / sigma;

    /* 2. two-sided CUSUM */
    state->cusum_high = state->cusum_high + z - cfg->cusum_slack;
    if (state->cusum_high < 0.0) { state->cusum_high = 0.0; }
    state->cusum_low = state->cusum_low - z - cfg->cusum_slack;
    if (state->cusum_low < 0.0) { state->cusum_low = 0.0; }

    state->n_seen++;
    in_warmup = (state->n_seen <= (uint64_t)cfg->warmup_samples);
    if (in_warmup) {
        flags |= CB_DET_WARMUP;
    } else {
        if (state->cusum_high >= cfg->cusum_threshold) { flags |= CB_DET_SHIFT_UP; }
        if (state->cusum_low  >= cfg->cusum_threshold) { flags |= CB_DET_SHIFT_DOWN; }
    }

    /* 3. baseline and scale update, unless held */
    if (cfg->baseline_hold_enabled != 0) {
        double worst = (state->cusum_high > state->cusum_low)
                     ? state->cusum_high : state->cusum_low;
        if (worst >= cfg->baseline_hold_frac * cfg->cusum_threshold) {
            hold_baseline = 1;
        }
    }
    if (hold_baseline) {
        flags |= CB_DET_BASELINE_HELD;
    } else {
        state->ewma     = state->ewma + cfg->ewma_alpha * residual;
        state->ewma_var = (1.0 - cfg->var_alpha) * state->ewma_var
                        + cfg->var_alpha * residual * residual;
        if (!cb_finite(state->ewma) || !cb_finite(state->ewma_var)) {
            /* Only reachable from inputs at the very edge of the double
             * range. Report rather than carry a NaN into later samples. */
            state->ewma     = value;
            state->ewma_var = cfg->min_sigma * cfg->min_sigma;
            return CB_ERR_NOT_FINITE;
        }
    }

    if (out != NULL) {
        out->flags               = flags;
        out->baseline            = state->ewma;
        out->sigma               = sigma;
        out->residual            = residual;
        out->normalised_residual = z;
        out->cusum_high          = state->cusum_high;
        out->cusum_low           = state->cusum_low;
    }
    return CB_OK;
}

cb_status_t cb_detector_reset_alarms(cb_detector_state_t *state)
{
    CB_REQUIRE(state != NULL, CB_ERR_NULL);
    CB_REQUIRE(state->magic == CB_MAGIC_DETECTOR, CB_ERR_STATE);
    state->cusum_high = 0.0;
    state->cusum_low  = 0.0;
    return CB_OK;
}

cb_status_t cb_detector_baseline(const cb_detector_state_t *state,
                                 const cb_detector_config_t *cfg,
                                 double *out_baseline,
                                 double *out_sigma)
{
    double sigma;

    CB_REQUIRE(state != NULL, CB_ERR_NULL);
    CB_CHECK(cb_detector_config_validate(cfg));
    CB_REQUIRE(state->magic == CB_MAGIC_DETECTOR, CB_ERR_STATE);

    sigma = sqrt(state->ewma_var);
    if (!(sigma > cfg->min_sigma)) {
        sigma = cfg->min_sigma;
    }
    if (out_baseline != NULL) { *out_baseline = state->ewma; }
    if (out_sigma != NULL)    { *out_sigma = sigma; }
    return CB_OK;
}

/* Siegmund's approximation for the one-sided CUSUM, used to pick a
 * threshold from a target false-alarm rate rather than by feel. */
cb_status_t cb_detector_arl0(double cusum_slack, double cusum_threshold, double *out_arl0)
{
    double b, x, num;

    CB_REQUIRE(out_arl0 != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(cusum_slack) && cb_finite(cusum_threshold), CB_ERR_NOT_FINITE);
    CB_REQUIRE(cusum_slack > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cusum_threshold > 0.0, CB_ERR_DOMAIN);

    b = cusum_threshold + 1.166;
    x = 2.0 * cusum_slack * b;
    if (x > 700.0) {
        return CB_ERR_CAPACITY; /* exp() would overflow; the ARL is astronomical */
    }
    num = exp(x) - x - 1.0;
    *out_arl0 = num / (2.0 * cusum_slack * cusum_slack);
    CB_REQUIRE(cb_finite(*out_arl0), CB_ERR_CAPACITY);
    return CB_OK;
}

cb_status_t cb_detector_arl1(double cusum_slack, double cusum_threshold,
                             double shift_sigmas, double *out_arl1)
{
    double d, b, x, num;

    CB_REQUIRE(out_arl1 != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(cusum_slack) && cb_finite(cusum_threshold) &&
               cb_finite(shift_sigmas), CB_ERR_NOT_FINITE);
    CB_REQUIRE(cusum_slack >= 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cusum_threshold > 0.0, CB_ERR_DOMAIN);

    d = shift_sigmas - cusum_slack;
    /* A shift inside the slack band is absorbed and never triggers; the
     * expected delay is unbounded, which is an answer, not a number. */
    CB_REQUIRE(d > 0.0, CB_ERR_DOMAIN);

    b = cusum_threshold + 1.166;
    x = -2.0 * d * b;
    num = exp(x) + (2.0 * d * b) - 1.0;
    *out_arl1 = num / (2.0 * d * d);
    CB_REQUIRE(cb_finite(*out_arl1), CB_ERR_CAPACITY);
    return CB_OK;
}
