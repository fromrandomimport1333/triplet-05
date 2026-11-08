/* Unit tests for citybrain/physics.h: Gaussian LRT, flood mass balance, fire
 * growth and plume dispersion, and the Greenshields traffic model. Worked
 * examples were computed independently in Python, noted above each case. */
#include "cb_test.h"

static void test_gaussian_lrt(void)
{
    double lr;

    /* Python: observed=5, h1=8, h0=0, sigma=2
     *   exponent = ((8-0)*((5-0)+(5-8))) / (2*4) = (8*2)/8 = 2.0
     *   lr = exp(2.0) = 7.38905609893065 */
    CB_CASE("worked example: observation closer to H1 gives LR > 1");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(5.0, 8.0, 0.0, 2.0, &lr));
    CB_CHECK_NEAR(lr, 7.38905609893065, 1e-12);

    CB_CASE("an observation exactly at H0 with H1 far away gives a tiny LR");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(0.0, 100.0, 0.0, 1.0, &lr));
    CB_CHECK_TRUE(lr < 1e-100);
    CB_CHECK_TRUE(lr >= 0.0 && !isnan(lr));

    CB_CASE("an observation exactly at H1 with H0 far away gives a huge LR");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(100.0, 100.0, 0.0, 1.0, &lr));
    CB_CHECK_TRUE(lr > 1e100 || lr == HUGE_VAL);
    CB_CHECK_TRUE(!isnan(lr));

    CB_CASE("equidistant from both hypotheses gives LR == 1");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(5.0, 10.0, 0.0, 3.0, &lr));
    CB_CHECK_NEAR(lr, 1.0, 1e-12);

    CB_CASE("H1 == H0 means the observation carries no evidentiary weight");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(42.0, 7.0, 7.0, 2.0, &lr));
    CB_CHECK_NEAR(lr, 1.0, 1e-12);

    CB_CASE("an extreme observation never crashes or produces NaN, only a very small/large LR");
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(1e18, 0.0, 0.0, 1.0, &lr));
    CB_CHECK_TRUE(!isnan(lr));
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(-1e18, 1e6, -1e6, 1.0, &lr));
    CB_CHECK_TRUE(!isnan(lr));

    CB_CASE("domain and finiteness errors");
    CB_CHECK_STATUS(cb_physics_gaussian_likelihood_ratio(1.0, 2.0, 0.0, 0.0, &lr),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_gaussian_likelihood_ratio(1.0, 2.0, 0.0, -1.0, &lr),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_gaussian_likelihood_ratio((double)NAN, 2.0, 0.0, 1.0, &lr),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_gaussian_likelihood_ratio(HUGE_VAL, 2.0, 0.0, 1.0, &lr),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_gaussian_likelihood_ratio(1.0, 2.0, 0.0, 1.0, NULL),
                    CB_ERR_NULL);
}

static void test_flood_rise_rate(void)
{
    double rate;

    /* Python: area=1000, inflow=5, coeff=0.5, exponent=1.5, level=2
     *   outflow = 0.5 * 2^1.5 = 1.4142135623730951
     *   rate = (5 - 1.4142135623730951) / 1000 = 0.0035857864376269 */
    CB_CASE("worked example: weir-type outflow exponent 1.5");
    CB_CHECK_OK(cb_physics_flood_rise_rate(1000.0, 5.0, 0.5, 1.5, 2.0, &rate));
    CB_CHECK_NEAR(rate, 0.0035857864376269049, 1e-12);

    CB_CASE("zero inflow and zero outflow coefficient gives zero rise");
    CB_CHECK_OK(cb_physics_flood_rise_rate(500.0, 0.0, 0.0, 1.5, 3.0, &rate));
    CB_CHECK_NEAR(rate, 0.0, 0.0);

    CB_CASE("outflow exceeding inflow gives a negative (falling) rate");
    CB_CHECK_OK(cb_physics_flood_rise_rate(100.0, 1.0, 5.0, 1.0, 10.0, &rate));
    CB_CHECK_TRUE(rate < 0.0);

    CB_CASE("water level of exactly zero is legal (an empty/dry zone)");
    CB_CHECK_OK(cb_physics_flood_rise_rate(100.0, 2.0, 1.0, 1.5, 0.0, &rate));
    CB_CHECK_NEAR(rate, 0.02, 1e-12); /* outflow = coeff*0^1.5 = 0 */

    CB_CASE("domain errors: area, exponent, level, and non-finite inputs");
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(0.0, 1.0, 0.5, 1.5, 1.0, &rate),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(-10.0, 1.0, 0.5, 1.5, 1.0, &rate),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(100.0, 1.0, -0.1, 1.5, 1.0, &rate),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(100.0, 1.0, 0.5, 0.0, 1.0, &rate),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(100.0, 1.0, 0.5, 1.5, -1.0, &rate),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate((double)NAN, 1.0, 0.5, 1.5, 1.0, &rate),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(100.0, 1.0, 0.5, 1.5, 1.0, NULL),
                    CB_ERR_NULL);

    CB_CASE("a negative water level is a model-domain error, not a trust judgement");
    /* The error is about pow()'s domain, not whether -1.0 m should be believed. */
    CB_CHECK_STATUS(cb_physics_flood_rise_rate(100.0, 1.0, 0.5, 1.5, -0.001, &rate),
                    CB_ERR_DOMAIN);
}

static void test_fire_growth_class(void)
{
    double alpha;

    CB_CASE("published NFPA 72 / SFPE t-squared growth constants");
    CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_SLOW, &alpha));
    CB_CHECK_NEAR(alpha, 0.00293, 1e-12);
    CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_MEDIUM, &alpha));
    CB_CHECK_NEAR(alpha, 0.0117, 1e-12);
    CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_FAST, &alpha));
    CB_CHECK_NEAR(alpha, 0.0469, 1e-12);
    CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_ULTRAFAST, &alpha));
    CB_CHECK_NEAR(alpha, 0.1876, 1e-12);

    CB_CASE("growth classes are strictly ordered, slow to ultrafast");
    {
        double s, m, f, u;
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_SLOW, &s));
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_MEDIUM, &m));
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_FAST, &f));
        CB_CHECK_OK(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_ULTRAFAST, &u));
        CB_CHECK_TRUE(s < m && m < f && f < u);
    }

    CB_CASE("an out-of-range growth class is a domain error");
    CB_CHECK_STATUS(cb_physics_fire_growth_class_alpha((cb_fire_growth_class_t)99, &alpha),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_fire_growth_class_alpha(CB_FIRE_GROWTH_SLOW, NULL),
                    CB_ERR_NULL);
}

static void test_fire_hrr(void)
{
    double hrr;

    /* Python: alpha=0.0117 (medium), t=120 -> hrr = 0.0117*120^2 = 168.48 */
    CB_CASE("worked example: medium growth class at 120 seconds");
    CB_CHECK_OK(cb_physics_fire_heat_release_rate(0.0117, 120.0, &hrr));
    CB_CHECK_NEAR(hrr, 168.48, 1e-9);

    CB_CASE("t = 0 gives zero heat release at effective ignition");
    CB_CHECK_OK(cb_physics_fire_heat_release_rate(0.1876, 0.0, &hrr));
    CB_CHECK_NEAR(hrr, 0.0, 0.0);

    CB_CASE("HRR is monotone increasing in time for a fixed growth class");
    {
        double a, b;
        CB_CHECK_OK(cb_physics_fire_heat_release_rate(0.0469, 50.0, &a));
        CB_CHECK_OK(cb_physics_fire_heat_release_rate(0.0469, 51.0, &b));
        CB_CHECK_TRUE(b > a);
    }

    CB_CASE("domain and finiteness errors");
    CB_CHECK_STATUS(cb_physics_fire_heat_release_rate(0.0, 10.0, &hrr), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_fire_heat_release_rate(-0.01, 10.0, &hrr), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_fire_heat_release_rate(0.01, -1.0, &hrr), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_fire_heat_release_rate((double)NAN, 10.0, &hrr),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_fire_heat_release_rate(0.01, 10.0, NULL), CB_ERR_NULL);
}

static void test_plume_concentration(void)
{
    double conc;

    /* Python: Q=10, u=2, sigma_y=50, sigma_z=20, y=0, z=0, H=0
     *   horiz = exp(0) = 1
     *   vertical = exp(0) + exp(0) = 2   (ground reflection doubles it)
     *   denom = 2*pi*2*50*20 = 12566.370614359172
     *   conc = (10/12566.370614359172)*1*2 = 0.0015915494309189535 */
    CB_CASE("worked example: ground-level centerline, zero effective height");
    CB_CHECK_OK(cb_physics_plume_concentration(10.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0, &conc));
    CB_CHECK_NEAR(conc, 0.0015915494309189535, 1e-12);

    CB_CASE("concentration falls off moving away from the plume centerline");
    {
        double on_axis, off_axis;
        CB_CHECK_OK(cb_physics_plume_concentration(10.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0,
                                                    &on_axis));
        CB_CHECK_OK(cb_physics_plume_concentration(10.0, 2.0, 50.0, 20.0, 100.0, 0.0, 0.0,
                                                    &off_axis));
        CB_CHECK_TRUE(off_axis < on_axis);
        CB_CHECK_TRUE(off_axis >= 0.0);
    }

    CB_CASE("zero emission rate gives zero concentration everywhere");
    CB_CHECK_OK(cb_physics_plume_concentration(0.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0, &conc));
    CB_CHECK_NEAR(conc, 0.0, 0.0);

    CB_CASE("calm wind is a documented model-domain error, not a crash");
    CB_CHECK_STATUS(cb_physics_plume_concentration(10.0, 0.0, 50.0, 20.0, 0.0, 0.0, 0.0,
                                                    &conc), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_plume_concentration(10.0, -1.0, 50.0, 20.0, 0.0, 0.0, 0.0,
                                                    &conc), CB_ERR_DOMAIN);

    CB_CASE("other domain and finiteness errors");
    CB_CHECK_STATUS(cb_physics_plume_concentration(-1.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0,
                                                    &conc), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_plume_concentration(10.0, 2.0, 0.0, 20.0, 0.0, 0.0, 0.0,
                                                    &conc), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_plume_concentration(10.0, 2.0, 50.0, 0.0, 0.0, 0.0, 0.0,
                                                    &conc), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_plume_concentration((double)NAN, 2.0, 50.0, 20.0, 0.0, 0.0,
                                                    0.0, &conc), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_plume_concentration(10.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0,
                                                    NULL), CB_ERR_NULL);
}

static void test_traffic_greenshields(void)
{
    double speed, flow;

    /* Python: v_free=30, k_jam=120, k=60 -> v=15, q=900
     *         k=30 -> v=22.5, q=675 */
    CB_CASE("worked example: half-jam density");
    CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 60.0, &speed));
    CB_CHECK_NEAR(speed, 15.0, 1e-12);
    CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 60.0, &flow));
    CB_CHECK_NEAR(flow, 900.0, 1e-12);

    CB_CASE("worked example: quarter-jam density");
    CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 30.0, &speed));
    CB_CHECK_NEAR(speed, 22.5, 1e-12);
    CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 30.0, &flow));
    CB_CHECK_NEAR(flow, 675.0, 1e-12);

    CB_CASE("zero density gives free-flow speed and zero flow");
    CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 0.0, &speed));
    CB_CHECK_NEAR(speed, 30.0, 1e-12);
    CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 0.0, &flow));
    CB_CHECK_NEAR(flow, 0.0, 0.0);

    CB_CASE("density at jam density gives zero speed and zero flow");
    CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 120.0, &speed));
    CB_CHECK_NEAR(speed, 0.0, 1e-12);
    CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 120.0, &flow));
    CB_CHECK_NEAR(flow, 0.0, 1e-9);

    CB_CASE("flow is maximised at half the jam density, the model's capacity point");
    {
        double q_quarter, q_half, q_three_quarter;
        CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 30.0, &q_quarter));
        CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 60.0, &q_half));
        CB_CHECK_OK(cb_physics_traffic_greenshields_flow(30.0, 120.0, 90.0, &q_three_quarter));
        CB_CHECK_TRUE(q_half > q_quarter);
        CB_CHECK_TRUE(q_half > q_three_quarter);
    }

    CB_CASE("density beyond jam density is evaluated, not rejected, and goes negative");
    /* The formula is evaluated exactly as given, not clamped or rejected. */
    CB_CHECK_OK(cb_physics_traffic_greenshields_speed(30.0, 120.0, 150.0, &speed));
    CB_CHECK_TRUE(speed < 0.0);
    CB_CHECK_NEAR(speed, 30.0 * (1.0 - 150.0 / 120.0), 1e-12);

    CB_CASE("domain and finiteness errors");
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_speed(0.0, 120.0, 30.0, &speed),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_speed(30.0, 0.0, 30.0, &speed),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_speed(30.0, 120.0, -1.0, &speed),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_speed((double)NAN, 120.0, 30.0, &speed),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_speed(30.0, 120.0, 30.0, NULL),
                    CB_ERR_NULL);
    CB_CHECK_STATUS(cb_physics_traffic_greenshields_flow(0.0, 120.0, 30.0, &flow),
                    CB_ERR_DOMAIN);
}

/* End-to-end example: physics output feeding straight into fusion as ordinary input. */
static void test_physics_feeds_fusion_unmodified(void)
{
    double expected_rate, lr;
    cb_fusion_config_t cfg;
    cb_fusion_result_t result;
    cb_evidence_t item;

    CB_CASE("a physics-derived likelihood ratio is ordinary fusion input");
    CB_CHECK_OK(cb_physics_flood_rise_rate(200.0, 12.0, 0.5, 1.5, 1.0, &expected_rate));
    CB_CHECK_OK(cb_physics_gaussian_likelihood_ratio(expected_rate, expected_rate,
                                                      0.0, 0.01, &lr));

    CB_CHECK_OK(cb_fusion_config_init(&cfg));
    item.likelihood_ratio = lr;
    item.weight = 1.0;
    item.group_id = 0;
    CB_CHECK_OK(cb_fusion_posterior_independent(&cfg, &item, 1, &result));
    CB_CHECK_TRUE(result.posterior > cfg.prior);
    CB_CHECK_TRUE(result.posterior >= 0.0 && result.posterior <= 1.0);

    /* Physics evidence gets no exemption from the cap. */
    CB_CHECK_TRUE(result.evidence_log_odds <= log(cfg.lr_cap) + 1e-9);
}

int main(void)
{
    test_gaussian_lrt();
    test_flood_rise_rate();
    test_fire_growth_class();
    test_fire_hrr();
    test_plume_concentration();
    test_traffic_greenshields();
    test_physics_feeds_fusion_unmodified();
    return cb_t_report("test_physics");
}
