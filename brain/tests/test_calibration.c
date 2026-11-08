/* Bounded calibration update (guarantee G5). */
#include "cb_test.h"

static void test_step(void)
{
    cb_calibration_bounds_t b;
    cb_calibration_result_t r;

    CB_CASE("a small gradient moves by rate times gradient");
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    CB_CHECK_OK(cb_calibration_step(&b, 0.5, 0.1, &r));
    CB_CHECK_NEAR(r.value, 0.51, 1e-15);
    CB_CHECK_NEAR(r.raw_step, 0.01, 1e-15);
    CB_CHECK_NEAR(r.applied_step, 0.01, 1e-15);
    CB_CHECK_EQ_INT(r.step_clipped, 0);
    CB_CHECK_EQ_INT(r.bound_clipped, 0);

    CB_CASE("a huge gradient is clipped to one step, in both directions");
    CB_CHECK_OK(cb_calibration_step(&b, 0.5, 1e9, &r));
    CB_CHECK_NEAR(r.value, 0.55, 1e-15);
    CB_CHECK_EQ_INT(r.step_clipped, 1);
    CB_CHECK_OK(cb_calibration_step(&b, 0.5, -1e9, &r));
    CB_CHECK_NEAR(r.value, 0.45, 1e-15);
    CB_CHECK_EQ_INT(r.step_clipped, 1);

    CB_CASE("the absolute bound holds at the edges");
    CB_CHECK_OK(cb_calibration_step(&b, 0.99, 1e9, &r));
    CB_CHECK_NEAR(r.value, 1.0, 0.0);
    CB_CHECK_EQ_INT(r.bound_clipped, 1);
    CB_CHECK_OK(cb_calibration_step(&b, 1.0, 1e9, &r));
    CB_CHECK_NEAR(r.value, 1.0, 0.0);
    CB_CHECK_NEAR(r.applied_step, 0.0, 0.0);
    CB_CHECK_OK(cb_calibration_step(&b, 0.0, -1e9, &r));
    CB_CHECK_NEAR(r.value, 0.0, 0.0);

    CB_CASE("a zero step size freezes the parameter completely");
    b.max_step = 0.0;
    CB_CHECK_OK(cb_calibration_step(&b, 0.5, 1e9, &r));
    CB_CHECK_NEAR(r.value, 0.5, 0.0);
    CB_CHECK_NEAR(r.applied_step, 0.0, 0.0);

    CB_CASE("a thousand hostile updates never leave the bounds");
    {
        int i;
        double theta = 0.5;
        CB_CHECK_OK(cb_calibration_bounds_init(&b));
        for (i = 0; i < 1000; ++i) {
            CB_CHECK_OK(cb_calibration_step(&b, theta, 1e12, &r));
            theta = r.value;
            CB_CHECK_TRUE(theta >= b.lo && theta <= b.hi);
        }
        CB_CHECK_NEAR(theta, b.hi, 0.0);
    }

    CB_CASE("a current value outside the bounds is an error, not silently pulled in");
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    CB_CHECK_STATUS(cb_calibration_step(&b, 1.5, 0.0, &r), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_calibration_step(&b, -0.5, 0.0, &r), CB_ERR_DOMAIN);

    CB_CASE("calibration argument domains");
    CB_CHECK_STATUS(cb_calibration_step(&b, 0.5, (double)NAN, &r), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_calibration_step(&b, 0.5, 0.0, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_calibration_step(NULL, 0.5, 0.0, &r), CB_ERR_NULL);
    b.max_step = -1.0;
    CB_CHECK_STATUS(cb_calibration_step(&b, 0.5, 0.0, &r), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    b.hi = -1.0;
    CB_CHECK_STATUS(cb_calibration_step(&b, 0.5, 0.0, &r), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    b.rate = HUGE_VAL;
    CB_CHECK_STATUS(cb_calibration_step(&b, 0.5, 0.0, &r), CB_ERR_NOT_FINITE);
}

static void test_min_steps(void)
{
    cb_calibration_bounds_t b;
    int32_t n;

    CB_CASE("poisoning cost is the distance divided by the step cap");
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    CB_CHECK_OK(cb_calibration_min_steps(&b, 0.5, 1.0, &n));
    CB_CHECK_EQ_INT(n, 10);
    CB_CHECK_OK(cb_calibration_min_steps(&b, 0.0, 1.0, &n));
    CB_CHECK_EQ_INT(n, 20);
    CB_CHECK_OK(cb_calibration_min_steps(&b, 1.0, 0.0, &n));
    CB_CHECK_EQ_INT(n, 20);

    CB_CASE("a partial step still costs one whole update");
    CB_CHECK_OK(cb_calibration_min_steps(&b, 0.5, 0.51, &n));
    CB_CHECK_EQ_INT(n, 1);
    CB_CHECK_OK(cb_calibration_min_steps(&b, 0.5, 0.5, &n));
    CB_CHECK_EQ_INT(n, 0);

    CB_CASE("a frozen parameter cannot be moved at all");
    b.max_step = 0.0;
    CB_CHECK_STATUS(cb_calibration_min_steps(&b, 0.0, 1.0, &n), CB_ERR_DOMAIN);
    CB_CHECK_OK(cb_calibration_min_steps(&b, 0.5, 0.5, &n));
    CB_CHECK_EQ_INT(n, 0);

    CB_CASE("an absurd distance is a capacity error, not an overflow");
    CB_CHECK_OK(cb_calibration_bounds_init(&b));
    b.max_step = 1e-300;
    CB_CHECK_STATUS(cb_calibration_min_steps(&b, 0.0, 1.0, &n), CB_ERR_CAPACITY);
    CB_CHECK_STATUS(cb_calibration_min_steps(&b, 0.0, 1.0, NULL), CB_ERR_NULL);
}

int main(void)
{
    test_step();
    test_min_steps();
    return cb_t_report("test_calibration");
}
