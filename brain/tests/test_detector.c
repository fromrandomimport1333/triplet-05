/* Unit and edge-case tests for the change-point detector. */
#include "cb_test.h"

/* Deterministic pseudo-noise so every run sees the same stream. A linear
 * congruential generator is enough here and keeps the test self-contained. */
typedef struct rng { uint64_t s; } rng_t;
static double rng_uniform(rng_t *r)
{
    r->s = r->s * 6364136223846793005ULL + 1442695040888963407ULL;
    return (double)((r->s >> 11) & 0x1FFFFFFFFFFFFFULL) / 9007199254740992.0;
}
static double rng_normal(rng_t *r)
{
    double u1 = rng_uniform(r);
    double u2 = rng_uniform(r);
    if (u1 < 1e-12) { u1 = 1e-12; }
    return sqrt(-2.0 * log(u1)) * cos(6.283185307179586 * u2);
}

static void test_config_validation(void)
{
    cb_detector_config_t cfg;

    CB_CASE("default config validates");
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    CB_CHECK_OK(cb_detector_config_validate(&cfg));

    CB_CASE("each config field has a checked domain");
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.ewma_alpha = 0.0;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.ewma_alpha = 1.5;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.var_alpha = 0.0;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.min_sigma = 0.0;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.cusum_slack = -0.1;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.cusum_threshold = 0.0;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.baseline_hold_frac = 1.5;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.baseline_hold_enabled = 7;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_detector_config_init(&cfg)); cfg.ewma_alpha = (double)NAN;
    CB_CHECK_STATUS(cb_detector_config_validate(&cfg), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_detector_config_validate(NULL), CB_ERR_NULL);
}

static void test_init_and_state_guard(void)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    cb_detector_output_t out;

    CB_CASE("uninitialised state is rejected, not dereferenced blindly");
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    memset(&st, 0, sizeof st);
    CB_CHECK_STATUS(cb_detector_update(&st, &cfg, 1.0, &out), CB_ERR_STATE);
    CB_CHECK_STATUS(cb_detector_reset_alarms(&st), CB_ERR_STATE);

    CB_CASE("init rejects a non-positive or non-finite seed");
    CB_CHECK_STATUS(cb_detector_init(&st, &cfg, 1.0, 0.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_detector_init(&st, &cfg, 1.0, -1.0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_detector_init(&st, &cfg, (double)NAN, 1.0), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_detector_init(NULL, &cfg, 1.0, 1.0), CB_ERR_NULL);

    CB_CHECK_OK(cb_detector_init(&st, &cfg, 10.0, 1.0));

    CB_CASE("a non-finite reading is an error and does not poison the state");
    CB_CHECK_STATUS(cb_detector_update(&st, &cfg, (double)NAN, &out),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_detector_update(&st, &cfg, HUGE_VAL, &out),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_OK(cb_detector_update(&st, &cfg, 10.0, &out));
    CB_CHECK_TRUE(isfinite(out.baseline) && isfinite(out.sigma));

    CB_CASE("a NULL output pointer is allowed");
    CB_CHECK_OK(cb_detector_update(&st, &cfg, 10.0, NULL));
}

/* False-alarm rate on a stationary stream, measured rather than assumed: the
 * Siegmund approximation in cb_detector_arl0 assumes a fixed baseline, but
 * this detector learns its own and stops learning once the CUSUM builds. */
static double measure_false_alarms(int hold_enabled, int seeds, int samples)
{
    long total = 0;
    int s;

    for (s = 0; s < seeds; ++s) {
        cb_detector_config_t cfg;
        cb_detector_state_t st;
        cb_detector_output_t out;
        rng_t r;
        int i;

        r.s = (uint64_t)(s * 7919 + 13);
        CB_CHECK_OK(cb_detector_config_init(&cfg));
        cfg.baseline_hold_enabled = hold_enabled;
        CB_CHECK_OK(cb_detector_init(&st, &cfg, 100.0, 1.0));
        for (i = 0; i < samples; ++i) {
            CB_CHECK_OK(cb_detector_update(&st, &cfg, 100.0 + rng_normal(&r), &out));
            if ((out.flags & (CB_DET_SHIFT_UP | CB_DET_SHIFT_DOWN)) != 0u) {
                total++;
                CB_CHECK_OK(cb_detector_reset_alarms(&st));
            }
        }
    }
    return (double)total / (double)seeds;
}

static void test_false_alarm_rate(void)
{
    const int seeds = 50;
    const int samples = 2000;
    double with_hold, without_hold, arl0, predicted;

    CB_CASE("a stationary stream's false-alarm rate stays inside its measured band");
    with_hold = measure_false_alarms(1, seeds, samples);
    without_hold = measure_false_alarms(0, seeds, samples);
    CB_CHECK_OK(cb_detector_arl0(0.5, 5.0, &arl0));
    predicted = 2.0 * (double)samples / arl0; /* two one-sided CUSUMs */

    printf("  stationary stream, k=0.5 h=5, %d seeds x %d samples:\n",
           seeds, samples);
    printf("    Siegmund prediction (fixed baseline): %.2f alarms per run\n",
           predicted);
    printf("    measured, baseline hold on:           %.2f alarms per run\n",
           with_hold);
    printf("    measured, baseline hold off:          %.2f alarms per run\n",
           without_hold);

    /* Bound is empirical with headroom; a CUSUM has a nonzero false-alarm rate
     * by construction, so a caller should corroborate an alarm, not act alone. */
    CB_CHECK_TRUE(with_hold > 0.0 && with_hold < 30.0);
    CB_CHECK_TRUE(without_hold < with_hold);
    CB_CHECK_TRUE(without_hold < 10.0);
}

static void test_step_change_detected(void)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    cb_detector_output_t out;
    rng_t r;
    int i, delay = -1;

    CB_CASE("a sustained three-sigma step is caught within 20 samples");
    r.s = 999u;
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 50.0, 1.0));
    for (i = 0; i < 200; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 50.0 + rng_normal(&r), &out));
    }
    CB_CHECK_OK(cb_detector_reset_alarms(&st));
    for (i = 0; i < 100; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 53.0 + rng_normal(&r), &out));
        if ((out.flags & CB_DET_SHIFT_UP) != 0u) { delay = i; break; }
    }
    CB_CHECK_TRUE(delay >= 0);
    CB_CHECK_TRUE(delay < 20);
    printf("  three-sigma step detected after %d samples\n", delay);

    CB_CASE("a downward step sets the down bit, not the up bit");
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 50.0, 1.0));
    for (i = 0; i < 200; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 50.0 + rng_normal(&r), &out));
    }
    CB_CHECK_OK(cb_detector_reset_alarms(&st));
    delay = -1;
    for (i = 0; i < 100; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 47.0 + rng_normal(&r), &out));
        if ((out.flags & CB_DET_SHIFT_DOWN) != 0u) { delay = i; break; }
    }
    CB_CHECK_TRUE(delay >= 0 && delay < 20);
    CB_CHECK_TRUE((out.flags & CB_DET_SHIFT_UP) == 0u);
}

static void test_warmup_suppression(void)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    cb_detector_output_t out;
    int i;
    uint32_t seen = 0u;

    CB_CASE("alarms are suppressed during warmup but the CUSUM still runs");
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    cfg.warmup_samples = 50u;
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 0.0, 1.0));
    for (i = 0; i < 50; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 100.0, &out));
        seen |= out.flags;
    }
    CB_CHECK_TRUE((seen & CB_DET_WARMUP) != 0u);
    CB_CHECK_TRUE((seen & (CB_DET_SHIFT_UP | CB_DET_SHIFT_DOWN)) == 0u);
    CB_CHECK_TRUE(out.cusum_high >= cfg.cusum_threshold);
    CB_CHECK_OK(cb_detector_update(&st, &cfg, 100.0, &out));
    CB_CHECK_TRUE((out.flags & CB_DET_SHIFT_UP) != 0u);
}

static void test_baseline_hold(void)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    cb_detector_output_t out;
    double baseline_with_hold, baseline_without;
    int i;

    CB_CASE("the baseline stops chasing a sustained ramp once the CUSUM builds");
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    cfg.warmup_samples = 0u;
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 0.0, 1.0));
    for (i = 0; i < 100; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 0.3 * (double)i, &out));
    }
    baseline_with_hold = out.baseline;
    CB_CHECK_TRUE((out.flags & CB_DET_BASELINE_HELD) != 0u);

    cfg.baseline_hold_enabled = 0;
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 0.0, 1.0));
    for (i = 0; i < 100; ++i) {
        CB_CHECK_OK(cb_detector_update(&st, &cfg, 0.3 * (double)i, &out));
    }
    baseline_without = out.baseline;
    CB_CHECK_TRUE((out.flags & CB_DET_BASELINE_HELD) == 0u);
    CB_CHECK_TRUE(baseline_with_hold < baseline_without);
    printf("  ramping stream baseline: held %.3f, free %.3f (true level %.3f)\n",
           baseline_with_hold, baseline_without, 0.3 * 99.0);
}

/* A slowly rising quantity never produces a large single-sample residual
 * against an adaptive baseline, so a per-sample threshold never fires;
 * accumulating the residual does. This is the case the detector exists for. */
static void test_slow_ramp(void)
{
    cb_detector_config_t cusum_cfg, naive_cfg;
    cb_detector_state_t cusum_st, naive_st;
    cb_detector_output_t out;
    rng_t r;
    int i;
    int cusum_at = -1;
    int naive_at = -1;
    const double ramp_per_sample = 0.05; /* sigma units */
    const double naive_threshold = 3.0;

    CB_CASE("a 0.05-sigma-per-sample ramp evades a 3-sigma per-sample rule");
    r.s = 2026u;

    /* Same adaptive baseline, but the decision is a bare per-sample
     * threshold on the residual -- the rule this detector replaces. */
    CB_CHECK_OK(cb_detector_config_init(&naive_cfg));
    naive_cfg.baseline_hold_enabled = 0;
    naive_cfg.cusum_threshold = 1e9; /* effectively disables the CUSUM alarm */
    CB_CHECK_OK(cb_detector_init(&naive_st, &naive_cfg, 0.0, 1.0));

    CB_CHECK_OK(cb_detector_config_init(&cusum_cfg));
    cusum_cfg.cusum_slack = 0.25; /* below the ramp's steady-state residual */
    CB_CHECK_OK(cb_detector_init(&cusum_st, &cusum_cfg, 0.0, 1.0));

    for (i = 0; i < 400; ++i) {
        double noise = rng_normal(&r);
        double value = ramp_per_sample * (double)i + noise;

        CB_CHECK_OK(cb_detector_update(&naive_st, &naive_cfg, value, &out));
        if (naive_at < 0 && fabs(out.normalised_residual) > naive_threshold) {
            naive_at = i;
        }
        CB_CHECK_OK(cb_detector_update(&cusum_st, &cusum_cfg, value, &out));
        if (cusum_at < 0 && (out.flags & CB_DET_SHIFT_UP) != 0u) {
            cusum_at = i;
        }
    }

    CB_CHECK_TRUE(cusum_at >= 0);
    CB_CHECK_EQ_INT(naive_at, -1);
    printf("  slow ramp: CUSUM alarmed at sample %d (true offset %.2f sigma); "
           "a 3-sigma per-sample rule never fired in 400 samples\n",
           cusum_at, ramp_per_sample * (double)cusum_at);
}

static void test_arl_helpers(void)
{
    double arl0, arl1;

    CB_CASE("ARL approximations land where the literature says they do");
    CB_CHECK_OK(cb_detector_arl0(0.5, 5.0, &arl0));
    /* The classic k = 0.5, h = 5 design point: in-control ARL in the high
     * hundreds, out-of-control delay around ten samples for a one-sigma
     * shift. Wide brackets: this is an approximation, not an identity. */
    CB_CHECK_TRUE(arl0 > 300.0 && arl0 < 1200.0);
    CB_CHECK_OK(cb_detector_arl1(0.5, 5.0, 1.0, &arl1));
    CB_CHECK_TRUE(arl1 > 5.0 && arl1 < 20.0);
    printf("  k=0.5 h=5: ARL0 ~= %.1f, ARL1(1 sigma) ~= %.2f\n", arl0, arl1);

    CB_CASE("a larger threshold trades delay for false alarms");
    {
        double arl0b, arl1b;
        CB_CHECK_OK(cb_detector_arl0(0.5, 8.0, &arl0b));
        CB_CHECK_OK(cb_detector_arl1(0.5, 8.0, 1.0, &arl1b));
        CB_CHECK_TRUE(arl0b > arl0);
        CB_CHECK_TRUE(arl1b > arl1);
    }

    CB_CASE("a shift inside the slack band has no finite expected delay");
    CB_CHECK_STATUS(cb_detector_arl1(0.5, 5.0, 0.5, &arl1), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_detector_arl1(0.5, 5.0, 0.2, &arl1), CB_ERR_DOMAIN);

    CB_CASE("ARL argument domains");
    CB_CHECK_STATUS(cb_detector_arl0(0.0, 5.0, &arl0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_detector_arl0(0.5, 0.0, &arl0), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_detector_arl0(0.5, 5.0, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_detector_arl0(0.5, 1e9, &arl0), CB_ERR_CAPACITY);
    CB_CHECK_STATUS(cb_detector_arl0((double)NAN, 5.0, &arl0), CB_ERR_NOT_FINITE);
}

static void test_baseline_accessor(void)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    double b, s;

    CB_CASE("baseline accessor reports the seed before any sample");
    CB_CHECK_OK(cb_detector_config_init(&cfg));
    CB_CHECK_OK(cb_detector_init(&st, &cfg, 42.0, 2.0));
    CB_CHECK_OK(cb_detector_baseline(&st, &cfg, &b, &s));
    CB_CHECK_NEAR(b, 42.0, 0.0);
    CB_CHECK_NEAR(s, 2.0, 1e-15);
    CB_CHECK_OK(cb_detector_baseline(&st, &cfg, NULL, NULL));
    CB_CHECK_STATUS(cb_detector_baseline(NULL, &cfg, &b, &s), CB_ERR_NULL);
}

int main(void)
{
    test_config_validation();
    test_init_and_state_guard();
    test_false_alarm_rate();
    test_step_change_detected();
    test_warmup_suppression();
    test_baseline_hold();
    test_slow_ramp();
    test_arl_helpers();
    test_baseline_accessor();
    return cb_t_report("test_detector");
}
