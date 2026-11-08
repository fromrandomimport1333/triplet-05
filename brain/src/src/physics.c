#include "citybrain/physics.h"
#include "internal.h"

/* M_PI is a POSIX/BSD extension to <math.h>, not part of strict C99; define
 * it locally so this file has no non-standard dependency. */
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Shared: Gaussian likelihood-ratio test */

cb_status_t cb_physics_gaussian_likelihood_ratio(double observed,
                                                  double expected_h1,
                                                  double expected_h0,
                                                  double sigma,
                                                  double *out_likelihood_ratio)
{
    double d0, d1, exponent, lr;

    CB_REQUIRE(out_likelihood_ratio != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(observed) && cb_finite(expected_h1) &&
               cb_finite(expected_h0) && cb_finite(sigma), CB_ERR_NOT_FINITE);
    CB_REQUIRE(sigma > 0.0, CB_ERR_DOMAIN);

    d0 = observed - expected_h0;
    d1 = observed - expected_h1;
    /* d0^2 - d1^2, via difference of squares, for precision. */
    exponent = ((expected_h1 - expected_h0) * (d0 + d1)) / (2.0 * sigma * sigma);

    if (exponent > 700.0) {
        /* exp() would overflow; report the largest finite value instead of inf. */
        *out_likelihood_ratio = HUGE_VAL;
        return CB_OK;
    }
    if (exponent < -700.0) {
        /* Underflows to zero; cb_fusion_posterior treats a zero ratio as its
         * own domain error, which is fusion's policy, not ours to work around. */
        *out_likelihood_ratio = 0.0;
        return CB_OK;
    }

    lr = exp(exponent);
    CB_REQUIRE(cb_finite(lr), CB_ERR_NOT_FINITE);
    *out_likelihood_ratio = lr;
    return CB_OK;
}

/* Flood: shallow-reservoir mass balance */

cb_status_t cb_physics_flood_rise_rate(double area_m2,
                                       double inflow_m3_per_s,
                                       double drainage_coefficient,
                                       double drainage_exponent,
                                       double water_level_m,
                                       double *out_rate_m_per_s)
{
    double outflow, rate;

    CB_REQUIRE(out_rate_m_per_s != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(area_m2) && cb_finite(inflow_m3_per_s) &&
               cb_finite(drainage_coefficient) && cb_finite(drainage_exponent) &&
               cb_finite(water_level_m), CB_ERR_NOT_FINITE);
    CB_REQUIRE(area_m2 > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(drainage_coefficient >= 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(drainage_exponent > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(water_level_m >= 0.0, CB_ERR_DOMAIN);

    outflow = drainage_coefficient * pow(water_level_m, drainage_exponent);
    rate = (inflow_m3_per_s - outflow) / area_m2;
    CB_REQUIRE(cb_finite(rate), CB_ERR_NOT_FINITE);

    *out_rate_m_per_s = rate;
    return CB_OK;
}

/* Fire: t-squared growth + Gaussian plume dispersion */

cb_status_t cb_physics_fire_growth_class_alpha(cb_fire_growth_class_t growth_class,
                                               double *out_alpha_kw_per_s2)
{
    CB_REQUIRE(out_alpha_kw_per_s2 != NULL, CB_ERR_NULL);
    switch (growth_class) {
    case CB_FIRE_GROWTH_SLOW:      *out_alpha_kw_per_s2 = 0.00293; return CB_OK;
    case CB_FIRE_GROWTH_MEDIUM:    *out_alpha_kw_per_s2 = 0.0117;  return CB_OK;
    case CB_FIRE_GROWTH_FAST:      *out_alpha_kw_per_s2 = 0.0469;  return CB_OK;
    case CB_FIRE_GROWTH_ULTRAFAST: *out_alpha_kw_per_s2 = 0.1876;  return CB_OK;
    default: return CB_ERR_DOMAIN;
    }
}

cb_status_t cb_physics_fire_heat_release_rate(double alpha_kw_per_s2,
                                              double time_s,
                                              double *out_hrr_kw)
{
    double hrr;

    CB_REQUIRE(out_hrr_kw != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(alpha_kw_per_s2) && cb_finite(time_s), CB_ERR_NOT_FINITE);
    CB_REQUIRE(alpha_kw_per_s2 > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(time_s >= 0.0, CB_ERR_DOMAIN);

    hrr = alpha_kw_per_s2 * time_s * time_s;
    CB_REQUIRE(cb_finite(hrr), CB_ERR_NOT_FINITE);

    *out_hrr_kw = hrr;
    return CB_OK;
}

cb_status_t cb_physics_plume_concentration(double emission_rate,
                                           double wind_speed_m_s,
                                           double sigma_y_m,
                                           double sigma_z_m,
                                           double crosswind_offset_m,
                                           double receptor_height_m,
                                           double effective_source_height_m,
                                           double *out_concentration)
{
    double horizontal, below, above, vertical, denom, conc;

    CB_REQUIRE(out_concentration != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(emission_rate) && cb_finite(wind_speed_m_s) &&
               cb_finite(sigma_y_m) && cb_finite(sigma_z_m) &&
               cb_finite(crosswind_offset_m) && cb_finite(receptor_height_m) &&
               cb_finite(effective_source_height_m), CB_ERR_NOT_FINITE);
    CB_REQUIRE(emission_rate >= 0.0, CB_ERR_DOMAIN);
    /* The Gaussian plume model is undefined in calm-wind conditions; see the
     * header comment. */
    CB_REQUIRE(wind_speed_m_s > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(sigma_y_m > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(sigma_z_m > 0.0, CB_ERR_DOMAIN);

    horizontal = exp(-(crosswind_offset_m * crosswind_offset_m) / (2.0 * sigma_y_m * sigma_y_m));
    below = receptor_height_m - effective_source_height_m;
    above = receptor_height_m + effective_source_height_m;
    vertical = exp(-(below * below) / (2.0 * sigma_z_m * sigma_z_m))
             + exp(-(above * above) / (2.0 * sigma_z_m * sigma_z_m));
    denom = 2.0 * M_PI * wind_speed_m_s * sigma_y_m * sigma_z_m;

    conc = (emission_rate / denom) * horizontal * vertical;
    CB_REQUIRE(cb_finite(conc), CB_ERR_NOT_FINITE);

    *out_concentration = conc;
    return CB_OK;
}

/* Traffic: Greenshields speed-density relation */

static cb_status_t traffic_validate(double free_flow_speed, double jam_density, double density)
{
    CB_REQUIRE(cb_finite(free_flow_speed) && cb_finite(jam_density) && cb_finite(density),
               CB_ERR_NOT_FINITE);
    CB_REQUIRE(free_flow_speed > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(jam_density > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(density >= 0.0, CB_ERR_DOMAIN);
    return CB_OK;
}

cb_status_t cb_physics_traffic_greenshields_speed(double free_flow_speed,
                                                   double jam_density,
                                                   double density,
                                                   double *out_speed)
{
    CB_REQUIRE(out_speed != NULL, CB_ERR_NULL);
    CB_CHECK(traffic_validate(free_flow_speed, jam_density, density));
    *out_speed = free_flow_speed * (1.0 - density / jam_density);
    return CB_OK;
}

cb_status_t cb_physics_traffic_greenshields_flow(double free_flow_speed,
                                                  double jam_density,
                                                  double density,
                                                  double *out_flow)
{
    CB_REQUIRE(out_flow != NULL, CB_ERR_NULL);
    CB_CHECK(traffic_validate(free_flow_speed, jam_density, density));
    *out_flow = free_flow_speed * density * (1.0 - density / jam_density);
    return CB_OK;
}
