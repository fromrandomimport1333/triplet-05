/*
 * Optional physical likelihood models for the fusion module: flood, fire
 * and traffic evidence grounded in published physics instead of a guessed
 * number. Every function answers "how consistent is this reading with a
 * real incident of this kind" and returns a likelihood ratio -- never a
 * verdict on whether the reading is trustworthy, which is a different
 * layer's job. This module doesn't change fusion's behaviour; a ratio
 * computed here is ordinary fusion input, nothing more.
 *
 * Every coefficient is a caller-supplied parameter. Where a published
 * reference table exists (the fire growth classes), it's exposed as a
 * separate lookup, not baked into the computation that uses it.
 */
#ifndef CITYBRAIN_PHYSICS_H
#define CITYBRAIN_PHYSICS_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Closed-form likelihood ratio between two Gaussian hypotheses with known
 * means and a shared standard deviation (Neyman-Pearson; Kay, "Fundamentals
 * of Statistical Signal Processing, Vol II," 1998, ch. 3). Every model
 * below builds on this one function rather than repeating the algebra.
 *
 * sigma is the caller's estimate of observation noise, same units as the
 * other arguments. Never errors on an implausible observed value -- an
 * observation far from both hypotheses is still well-defined. One known
 * limitation: a value that overshoots even the "incident" hypothesis can
 * swing the ratio toward favouring it anyway, since this only compares
 * relative distance to two points and doesn't independently bound
 * plausibility under either one. See docs/math_reasoning.md.
 *
 * Errors: CB_ERR_DOMAIN if sigma <= 0; CB_ERR_NOT_FINITE for a bad input.
 */
CB_API cb_status_t cb_physics_gaussian_likelihood_ratio(double observed,
                                                         double expected_h1,
                                                         double expected_h0,
                                                         double sigma,
                                                         double *out_likelihood_ratio);

/*
 * Expected rate of water-level rise for a zone modelled as a shallow,
 * well-mixed reservoir: dh/dt = (Q_in - Q_out(h)) / A, with
 * Q_out(h) = drainage_coefficient * h^drainage_exponent (1.5 for a weir,
 * 0.5 for a submerged orifice -- depends on the zone's actual drainage, so
 * it's a parameter). Assumes one representative water level per zone and
 * constant inflow over the timestep; not valid for a large or
 * non-uniformly flooding zone.
 *
 * Errors: CB_ERR_DOMAIN for area_m2 <= 0, drainage_coefficient < 0,
 * drainage_exponent <= 0, or water_level_m < 0 (the power law is
 * undefined for a negative base -- a model-domain limit, not a judgement
 * on the reading).
 */
CB_API cb_status_t cb_physics_flood_rise_rate(double area_m2,
                                              double inflow_m3_per_s,
                                              double drainage_coefficient,
                                              double drainage_exponent,
                                              double water_level_m,
                                              double *out_rate_m_per_s);

/* Published NFPA 72 / SFPE t-squared fire growth classes (kW/s^2). A
 * reference lookup only; the HRR function below takes alpha as an
 * ordinary parameter. */
typedef enum cb_fire_growth_class {
    CB_FIRE_GROWTH_SLOW      = 0, /* 0.00293 kW/s^2, ~600s to 1MW */
    CB_FIRE_GROWTH_MEDIUM    = 1, /* 0.0117  kW/s^2, ~300s to 1MW */
    CB_FIRE_GROWTH_FAST      = 2, /* 0.0469  kW/s^2, ~150s to 1MW */
    CB_FIRE_GROWTH_ULTRAFAST = 3  /* 0.1876  kW/s^2, ~75s  to 1MW */
} cb_fire_growth_class_t;

CB_API cb_status_t cb_physics_fire_growth_class_alpha(cb_fire_growth_class_t growth_class,
                                                       double *out_alpha_kw_per_s2);

/* t-squared heat release rate: Q(t) = alpha * t^2 (kW). Models free growth
 * only, not flashover, a limited plateau, or decay. Errors: CB_ERR_DOMAIN
 * for alpha <= 0 or time_s < 0. */
CB_API cb_status_t cb_physics_fire_heat_release_rate(double alpha_kw_per_s2,
                                                     double time_s,
                                                     double *out_hrr_kw);

/*
 * Ground-reflected Gaussian plume concentration at a receptor downwind of
 * a continuous source (Turner, "Workbook of Atmospheric Dispersion
 * Estimates," EPA, 1970). sigma_y/sigma_z are taken directly as
 * parameters; deriving them from distance and an atmospheric stability
 * class is the caller's job. Undefined for zero wind speed, a known limit
 * of the model, not an artefact here.
 *
 * Errors: CB_ERR_DOMAIN for emission_rate < 0, wind_speed_m_s <= 0,
 * sigma_y_m <= 0, or sigma_z_m <= 0.
 */
CB_API cb_status_t cb_physics_plume_concentration(double emission_rate,
                                                   double wind_speed_m_s,
                                                   double sigma_y_m,
                                                   double sigma_z_m,
                                                   double crosswind_offset_m,
                                                   double receptor_height_m,
                                                   double effective_source_height_m,
                                                   double *out_concentration);

/*
 * Greenshields' linear speed-density relation: v(k) = v_free*(1 - k/k_jam)
 * (Greenshields, "A Study of Traffic Capacity," Highway Research Board
 * Proceedings, vol. 14, 1935). Only meaningful for 0 <= density <=
 * jam_density; outside that range the formula is still evaluated rather
 * than rejected, and the resulting mismatch is itself what the Gaussian
 * ratio above is for. Errors: CB_ERR_DOMAIN for free_flow_speed <= 0,
 * jam_density <= 0, or density < 0.
 */
CB_API cb_status_t cb_physics_traffic_greenshields_speed(double free_flow_speed,
                                                          double jam_density,
                                                          double density,
                                                          double *out_speed);

/* Flow implied by the same relation: q(k) = k * v(k). */
CB_API cb_status_t cb_physics_traffic_greenshields_flow(double free_flow_speed,
                                                         double jam_density,
                                                         double density,
                                                         double *out_flow);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_PHYSICS_H */
