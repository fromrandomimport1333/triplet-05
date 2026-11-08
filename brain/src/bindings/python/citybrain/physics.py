"""
Wrapper for citybrain/physics.h: optional physical likelihood models for
the fusion module (flood, fire, traffic). Each function returns a
likelihood ratio describing how consistent a reading is with an incident,
never a verdict on whether the reading is trustworthy.
"""
from __future__ import annotations

from enum import IntEnum

from citybrain._raw import ffi, lib
from citybrain.errors import check


class FireGrowthClass(IntEnum):
    """Published NFPA 72 / SFPE t-squared fire growth classes."""
    SLOW = 0
    MEDIUM = 1
    FAST = 2
    ULTRAFAST = 3


def gaussian_likelihood_ratio(observed: float, expected_h1: float,
                               expected_h0: float, sigma: float) -> float:
    """Closed-form likelihood ratio between two Gaussian hypotheses with
    known means and a shared standard deviation (Neyman-Pearson; Kay 1998,
    ch. 3). sigma is the observation noise estimate, same units as the
    other arguments."""
    out = ffi.new("double *")
    check(lib.cb_physics_gaussian_likelihood_ratio(observed, expected_h1,
                                                    expected_h0, sigma, out))
    return out[0]


def flood_rise_rate(area_m2: float, inflow_m3_per_s: float,
                     drainage_coefficient: float, drainage_exponent: float,
                     water_level_m: float) -> float:
    """Expected water-level rise rate for a shallow, well-mixed reservoir
    (Maidment 1993; Chow 1959 for the power-law outflow relation)."""
    out = ffi.new("double *")
    check(lib.cb_physics_flood_rise_rate(area_m2, inflow_m3_per_s,
                                         drainage_coefficient, drainage_exponent,
                                         water_level_m, out))
    return out[0]


def fire_growth_class_alpha(growth_class: FireGrowthClass) -> float:
    """Published t-squared fire growth coefficient (kW/s^2), NFPA 72 /
    Karlsson & Quintiere 2000."""
    out = ffi.new("double *")
    check(lib.cb_physics_fire_growth_class_alpha(int(growth_class), out))
    return out[0]


def fire_heat_release_rate(alpha_kw_per_s2: float, time_s: float) -> float:
    """t-squared heat release rate: Q(t) = alpha * t^2 (kW)."""
    out = ffi.new("double *")
    check(lib.cb_physics_fire_heat_release_rate(alpha_kw_per_s2, time_s, out))
    return out[0]


def plume_concentration(emission_rate: float, wind_speed_m_s: float,
                         sigma_y_m: float, sigma_z_m: float,
                         crosswind_offset_m: float, receptor_height_m: float,
                         effective_source_height_m: float) -> float:
    """Ground-reflected Gaussian plume concentration at a receptor
    (Turner 1970). sigma_y/sigma_z are taken as direct parameters."""
    out = ffi.new("double *")
    check(lib.cb_physics_plume_concentration(emission_rate, wind_speed_m_s,
                                             sigma_y_m, sigma_z_m,
                                             crosswind_offset_m, receptor_height_m,
                                             effective_source_height_m, out))
    return out[0]


def traffic_greenshields_speed(free_flow_speed: float, jam_density: float,
                                density: float) -> float:
    """Greenshields' linear speed-density relation: v = v_free*(1-k/k_jam)
    (Greenshields 1935). Evaluated as given outside 0 <= density <=
    jam_density, not rejected."""
    out = ffi.new("double *")
    check(lib.cb_physics_traffic_greenshields_speed(free_flow_speed, jam_density,
                                                     density, out))
    return out[0]


def traffic_greenshields_flow(free_flow_speed: float, jam_density: float,
                               density: float) -> float:
    """Flow implied by the same relation: q = k * v(k)."""
    out = ffi.new("double *")
    check(lib.cb_physics_traffic_greenshields_flow(free_flow_speed, jam_density,
                                                    density, out))
    return out[0]
