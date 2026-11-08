/*
 * Change-point detection on a stream of measurements: an adaptive EWMA
 * baseline with a two-sided CUSUM on the normalised residual.
 *
 * This answers whether a stream has shown a sustained shift, not whether
 * the stream can be trusted -- authenticity and sensor health are a
 * different layer's problem and are not touched here.
 *
 * The EWMA learns each stream's own baseline and scale instead of relying
 * on a fixed threshold, which a generic library cannot know in advance.
 * CUSUM accumulates small deviations over time, so a gradual shift that
 * never trips a single-sample test still crosses the alarm threshold
 * eventually. The optional baseline hold freezes the baseline once the
 * CUSUM has built up evidence of a shift, so the shift stays visible
 * instead of being absorbed back into "normal" -- at the cost of a higher
 * false-alarm rate, which is measured in docs/testing.md.
 */
#ifndef CITYBRAIN_DETECTOR_H
#define CITYBRAIN_DETECTOR_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_DET_NONE          0x00u
#define CB_DET_WARMUP        0x01u
#define CB_DET_SHIFT_UP      0x02u
#define CB_DET_SHIFT_DOWN    0x04u
#define CB_DET_BASELINE_HELD 0x08u

typedef struct cb_detector_config {
    double   ewma_alpha;      /* baseline smoothing, in (0, 1] */
    double   var_alpha;       /* variance smoothing, in (0, 1] */
    double   min_sigma;       /* floor on the scale estimate, > 0 */
    double   cusum_slack;     /* k, per-sample drift treated as noise, >= 0 */
    double   cusum_threshold; /* h, alarm threshold, > 0 */
    int32_t  baseline_hold_enabled;
    double   baseline_hold_frac; /* fraction of h that triggers the hold, in [0,1] */
    uint32_t warmup_samples;  /* samples before alarms are reported */
} cb_detector_config_t;

typedef struct cb_detector_state {
    double   ewma;
    double   ewma_var;
    double   cusum_high;
    double   cusum_low;
    uint64_t n_seen;
    uint32_t magic;
} cb_detector_state_t;

typedef struct cb_detector_output {
    uint32_t flags;
    double   baseline;
    double   sigma;
    double   residual;
    double   normalised_residual;
    double   cusum_high;
    double   cusum_low;
} cb_detector_output_t;

/* Default: ewma_alpha=0.10, var_alpha=0.05, min_sigma=1e-6,
 * cusum_slack=0.5, cusum_threshold=5.0, baseline_hold_enabled=1,
 * baseline_hold_frac=0.5, warmup_samples=20. */
CB_API cb_status_t cb_detector_config_init(cb_detector_config_t *cfg);

CB_API cb_status_t cb_detector_config_validate(const cb_detector_config_t *cfg);

/* Seed a detector's state. first_value is the initial baseline;
 * initial_sigma must be > 0. */
CB_API cb_status_t cb_detector_init(cb_detector_state_t *state,
                                    const cb_detector_config_t *cfg,
                                    double first_value,
                                    double initial_sigma);

/* Feed one measurement: residual against the current baseline, CUSUM
 * update and alarm check, then baseline update unless held. out may be
 * NULL. Errors: CB_ERR_STATE if not initialised, CB_ERR_NOT_FINITE for a
 * bad value. */
CB_API cb_status_t cb_detector_update(cb_detector_state_t *state,
                                      const cb_detector_config_t *cfg,
                                      double value,
                                      cb_detector_output_t *out);

/* Clear both CUSUM accumulators, keep the learned baseline. */
CB_API cb_status_t cb_detector_reset_alarms(cb_detector_state_t *state);

/* Read the current baseline and scale without feeding a sample. */
CB_API cb_status_t cb_detector_baseline(const cb_detector_state_t *state,
                                        const cb_detector_config_t *cfg,
                                        double *out_baseline,
                                        double *out_sigma);

/* Average run length to false alarm, via Siegmund's approximation. An
 * approximation, not exact. */
CB_API cb_status_t cb_detector_arl0(double cusum_slack,
                                    double cusum_threshold,
                                    double *out_arl0);

/* Expected detection delay for a sustained shift of shift_sigmas standard
 * deviations. CB_ERR_DOMAIN if shift_sigmas <= cusum_slack, since a shift
 * inside the slack band never triggers. */
CB_API cb_status_t cb_detector_arl1(double cusum_slack,
                                    double cusum_threshold,
                                    double shift_sigmas,
                                    double *out_arl1);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_DETECTOR_H */
