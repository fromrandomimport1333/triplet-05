#!/usr/bin/env python3
"""
Independent generator for golden test vectors.

This computes expected outputs from the mathematical definitions directly in
Python, using Decimal for high precision, WITHOUT calling the citybrain C
library. The C tests and the Python binding tests both check their respective
implementations against this one independently-derived file, so agreement
between "C result" and "Python result" is evidence of correctness, not a
tautology (it would be a tautology if the vectors were dumped from the
library being tested).

Output: tests/vectors/golden.csv, one row per case, with a header naming the
function and its fields. Different functions have different column counts, so
the file is organized in blocks, each with its own header line starting with
'#function_name'.
"""
import csv
import math
import pathlib
from decimal import Decimal, getcontext

getcontext().prec = 50

def logit(p):
    p = Decimal(p)
    return (p / (1 - p)).ln()

def logistic(L):
    L = Decimal(L)
    if L >= 0:
        e = (-L).exp()
        return 1 / (1 + e)
    else:
        e = L.exp()
        return e / (1 + e)

rows = {}

# ---------------------------------------------------------------- fusion ---
# cb_fusion_posterior_independent: cfg(lr_cap, prior), items[(lr, weight)],
# -> posterior, evidence_log_odds
fusion_cases = []

def fusion_case(name, lr_cap, prior, items):
    ln_cap = Decimal(lr_cap).ln()
    L0 = logit(prior)
    total = Decimal(0)
    n_clamped = 0
    for lr, w in items:
        raw = Decimal(lr).ln()
        capped = max(-ln_cap, min(ln_cap, raw))
        if raw > ln_cap or raw < -ln_cap:
            n_clamped += 1
        total += Decimal(w) * capped
    L = L0 + total
    post = logistic(L)
    fusion_cases.append({
        "name": name, "lr_cap": lr_cap, "prior": prior,
        "n_items": len(items), "evidence_log_odds": total,
        "posterior": post, "n_items_clamped": n_clamped,
    })

fusion_case("empty", 8.0, 0.02, [])
fusion_case("one_item_lr_equal_cap", 8.0, 0.02, [(8.0, 1.0)])
fusion_case("one_item_above_cap_clips", 8.0, 0.02, [(1e30, 1.0)])
fusion_case("one_item_below_floor_clips", 8.0, 0.02, [(1e-30, 1.0)])
fusion_case("weighted_half", 8.0, 0.02, [(8.0, 0.5)])
fusion_case("weight_zero_silences", 8.0, 0.02, [(1e9, 0.0)])
fusion_case("lr_one_no_effect", 8.0, 0.02, [(1.0, 1.0)])
fusion_case("two_cancel", 8.0, 0.02, [(4.0, 1.0), (0.25, 1.0)])
fusion_case("brief_worked_example_one_source", 8.0, 0.02, [(1e300, 1.0)])
fusion_case("brief_worked_example_two_sources", 8.0, 0.02, [(1e300, 1.0), (1e300, 1.0)])
fusion_case("three_mixed", 5.0, 0.10, [(3.0, 1.0), (0.4, 0.5), (10.0, 0.25)])
fusion_case("high_prior", 8.0, 0.5, [(2.0, 1.0)])
fusion_case("tiny_prior", 8.0, 1e-6, [(8.0, 1.0)])
fusion_case("cap_near_one", 1.01, 0.3, [(100.0, 1.0)])
fusion_case("large_cap", 1000.0, 0.02, [(50.0, 1.0)])

rows["fusion"] = fusion_cases

# ------------------------------------------------------- fault_tolerance ---
ft_cases = []

def ft_min_sources(lr_cap, prior, threshold, max_weight=1.0, odds_mult=1.0):
    L0 = logit(prior) + Decimal(odds_mult).ln()
    step = Decimal(max_weight) * Decimal(lr_cap).ln()
    target = logit(threshold)
    if L0 >= target:
        return 0
    n = 0
    while L0 + n * step < target:
        n += 1
        if n > 100000:
            raise RuntimeError("no convergence")
    return n

def ft_max_posterior(lr_cap, prior, n, max_weight=1.0, odds_mult=1.0):
    L0 = logit(prior) + Decimal(odds_mult).ln()
    step = Decimal(max_weight) * Decimal(lr_cap).ln()
    return logistic(L0 + n * step)

def ft_case(name, lr_cap, prior, threshold, max_weight=1.0, odds_mult=1.0):
    n_min = ft_min_sources(lr_cap, prior, threshold, max_weight, odds_mult)
    p_at_n = ft_max_posterior(lr_cap, prior, n_min, max_weight, odds_mult)
    p_at_n_minus_1 = ft_max_posterior(lr_cap, prior, n_min - 1, max_weight, odds_mult) if n_min > 0 else None
    ft_cases.append({
        "name": name, "lr_cap": lr_cap, "prior": prior, "threshold": threshold,
        "max_weight": max_weight, "odds_multiplier": odds_mult,
        "n_min_sources": n_min,
        "posterior_at_n_min": p_at_n,
        "posterior_at_n_min_minus_1": p_at_n_minus_1 if p_at_n_minus_1 is not None else "",
    })

ft_case("design_brief_example", 8.0, 0.02, 0.20)
ft_case("bigger_cap_fewer_sources", 100.0, 0.02, 0.20)
ft_case("smaller_cap_more_sources", 1.5, 0.02, 0.20)
ft_case("lower_weight_more_sources", 8.0, 0.02, 0.20, max_weight=0.5)
ft_case("prior_already_above_threshold", 8.0, 0.5, 0.2)
ft_case("suppressed_by_graph_floor", 8.0, 0.02, 0.20, odds_mult=0.5)
ft_case("raised_by_graph", 8.0, 0.02, 0.20, odds_mult=3.0)
ft_case("high_threshold", 8.0, 0.02, 0.90)
ft_case("low_threshold", 8.0, 0.02, 0.03)
ft_case("tiny_prior", 5.0, 1e-6, 0.5)

rows["fault_tolerance"] = ft_cases

# ------------------------------------------------------------- decision ---
decision_cases = []

def decision_case(name, cost_act_false, cost_hold_true, cost_act_true, cost_hold_false, posterior):
    # Deliberately plain Python float (IEEE binary64), matching the C
    # library's double precision exactly -- NOT Decimal. The decision rule's
    # tie-break ("an exact tie holds") is a statement about float64 equality:
    # at posterior = 1/21, ec_act and ec_hold are bit-identical in binary64
    # but differ in the last digit under 50-digit Decimal arithmetic, because
    # 1/21 has no finite binary or decimal expansion. Using Decimal here would
    # make the vector generator a stricter (and different) oracle than the
    # library it is meant to check, so this section intentionally does not
    # use the module-wide Decimal precision.
    p = float(posterior)
    ec_act = p * float(cost_act_true) + (1 - p) * float(cost_act_false)
    ec_hold = p * float(cost_hold_true) + (1 - p) * float(cost_hold_false)
    action = "ACT" if ec_act < ec_hold else "HOLD"
    denom = (float(cost_hold_true) - float(cost_hold_false)) + (float(cost_act_false) - float(cost_act_true))
    if denom > 0:
        thresh = (float(cost_act_false) - float(cost_hold_false)) / denom
    else:
        thresh = -1.0
    decision_cases.append({
        "name": name, "cost_act_when_false": cost_act_false,
        "cost_hold_when_true": cost_hold_true, "cost_act_when_true": cost_act_true,
        "cost_hold_when_false": cost_hold_false, "posterior": posterior,
        "expected_cost_act": ec_act, "expected_cost_hold": ec_hold,
        "action": action, "indifference_threshold": thresh,
    })

decision_case("default_below_threshold", 1.0, 20.0, 0.0, 0.0, 0.04)
decision_case("default_above_threshold", 1.0, 20.0, 0.0, 0.0, 0.06)
decision_case("default_at_quarter", 1.0, 20.0, 0.0, 0.0, 0.25)
decision_case("exact_tie", 1.0, 20.0, 0.0, 0.0, 1.0/21.0)
decision_case("posterior_zero", 1.0, 20.0, 0.0, 0.0, 0.0)
decision_case("posterior_one", 1.0, 20.0, 0.0, 0.0, 1.0)
decision_case("negative_cost_as_benefit", 1.0, 20.0, -5.0, 0.0, 0.5)
decision_case("dominated_action", 0.0, 0.0, 0.0, 0.0, 0.5)

rows["decision"] = decision_cases

# ----------------------------------------------------------- calibration ---
calib_cases = []

def calib_case(name, lo, hi, max_step, rate, current, gradient):
    raw = Decimal(rate) * Decimal(gradient)
    stepped = max(-Decimal(max_step), min(Decimal(max_step), raw))
    candidate = Decimal(current) + stepped
    final = max(Decimal(lo), min(Decimal(hi), candidate))
    calib_cases.append({
        "name": name, "lo": lo, "hi": hi, "max_step": max_step, "rate": rate,
        "current": current, "gradient": gradient,
        "raw_step": raw, "applied_step": final - Decimal(current), "value": final,
        "step_clipped": 1 if raw != stepped else 0,
        "bound_clipped": 1 if candidate != final else 0,
    })

calib_case("small_gradient", 0.0, 1.0, 0.05, 0.1, 0.5, 0.1)
calib_case("huge_gradient_clips_step", 0.0, 1.0, 0.05, 0.1, 0.5, 1e9)
calib_case("huge_negative_gradient", 0.0, 1.0, 0.05, 0.1, 0.5, -1e9)
calib_case("hits_upper_bound", 0.0, 1.0, 0.05, 0.1, 0.99, 1e9)
calib_case("hits_lower_bound", 0.0, 1.0, 0.05, 0.1, 0.0, -1e9)
calib_case("zero_step_freezes", 0.0, 1.0, 0.0, 0.1, 0.5, 1e9)
calib_case("negative_rate", -10.0, 10.0, 1.0, -2.0, 0.0, 5.0)

rows["calibration"] = calib_cases

# ------------------------------------------------------------------ graph --
graph_cases = []

def clip(x, lo, hi):
    return max(lo, min(hi, x))

def graph_case(name, m_min, m_max, path_multipliers):
    """path_multipliers: list of raw edge multipliers along a path (already
    individually clipped, as cb_graph_set_edge would store them)."""
    product = Decimal(1)
    for m in path_multipliers:
        stored = clip(Decimal(m), Decimal(m_min), Decimal(m_max))
        product *= stored
    composed = clip(product, Decimal(m_min), Decimal(m_max))
    graph_cases.append({
        "name": name, "m_min": m_min, "m_max": m_max,
        "n_edges": len(path_multipliers),
        "unclipped_product": product, "composed_multiplier": composed,
    })

graph_case("single_lowers_edge", 0.5, 3.0, [0.5])
graph_case("seven_lowers_chain", 0.5, 3.0, [0.5]*7)
graph_case("seven_raises_chain", 0.5, 3.0, [3.0]*7)
graph_case("mixed_chain", 0.5, 3.0, [0.7, 2.0, 0.7, 2.0, 0.7, 2.0, 0.7])
graph_case("flood_accident_jam", 0.5, 3.0, [1.5, 3.0])
graph_case("flood_fire_jam", 0.5, 3.0, [0.5, 1.5])

rows["graph"] = graph_cases

# -------------------------------------------------------------- cache ----
cache_cases = []

def fnv1a64(fields):
    h = 0xcbf29ce484222325
    for v in fields:
        for i in range(8):
            byte = (v >> (i*8)) & 0xFF
            h ^= byte
            h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h

def cache_case(name, category_id, partition_id, bucket_id, state_version):
    h = fnv1a64([category_id & 0xFFFFFFFF, partition_id & 0xFFFFFFFF,
                 bucket_id & 0xFFFFFFFF, state_version & 0xFFFFFFFFFFFFFFFF])
    cache_cases.append({
        "name": name, "category_id": category_id, "partition_id": partition_id,
        "bucket_id": bucket_id, "state_version": state_version, "hash": h,
    })

cache_case("all_zero", 0, 0, 0, 0)
cache_case("typical", 1, 7, 2, 99)
cache_case("large_state_version", 1, 7, 2, 0xFFFFFFFFFFFF)
cache_case("max_int32_fields", 2147483647, 2147483647, 2147483647, 1)

rows["cache"] = cache_cases


# ------------------------------------------------------------------ physics --
# Independent reimplementation of citybrain/physics.h using Decimal
# arithmetic -- does NOT call the C library, for the same reason every
# other section in this file doesn't.
physics_cases = []

def dpow(base, exponent):
    # Decimal has no native fractional power; use float math.pow via
    # Decimal(repr(...)) round-trip only for the exponent step, keeping the
    # rest of the pipeline in Decimal. Fractional powers of a Decimal are
    # inherently a floating-point operation in Python's standard library
    # (Decimal.__pow__ requires integer exponents for arbitrary precision),
    # so this one operation uses math.pow at double precision, same as the
    # C function being checked against -- documented here rather than
    # silently mixed in.
    return Decimal(repr(math.pow(float(base), float(exponent))))

def gaussian_lrt(observed, expected_h1, expected_h0, sigma):
    observed = Decimal(observed); expected_h1 = Decimal(expected_h1)
    expected_h0 = Decimal(expected_h0); sigma = Decimal(sigma)
    d0 = observed - expected_h0
    d1 = observed - expected_h1
    exponent = ((expected_h1 - expected_h0) * (d0 + d1)) / (2 * sigma * sigma)
    return exponent.exp()

def physics_gaussian_case(name, observed, h1, h0, sigma):
    lr = gaussian_lrt(observed, h1, h0, sigma)
    physics_cases.append({
        "kind": "gaussian_lrt", "name": name,
        "observed": observed, "expected_h1": h1, "expected_h0": h0, "sigma": sigma,
        "result": lr,
    })

physics_gaussian_case("worked_example", 5.0, 8.0, 0.0, 2.0)
physics_gaussian_case("equidistant_gives_one", 5.0, 10.0, 0.0, 3.0)
physics_gaussian_case("h1_equals_h0_no_weight", 42.0, 7.0, 7.0, 2.0)

rows["physics_gaussian_lrt"] = physics_cases
physics_cases = []

def flood_rise_rate(area, inflow, coeff, exponent, level):
    area = Decimal(area); inflow = Decimal(inflow)
    coeff = Decimal(coeff); level = Decimal(level)
    outflow = coeff * dpow(level, exponent)
    return (inflow - outflow) / area

def physics_flood_case(name, area, inflow, coeff, exponent, level):
    rate = flood_rise_rate(area, inflow, coeff, exponent, level)
    physics_cases.append({
        "kind": "flood_rise_rate", "name": name,
        "area_m2": area, "inflow_m3_per_s": inflow,
        "drainage_coefficient": coeff, "drainage_exponent": exponent,
        "water_level_m": level, "result": rate,
    })

physics_flood_case("worked_example", 1000.0, 5.0, 0.5, 1.5, 2.0)
physics_flood_case("zero_level_zero_outflow", 100.0, 2.0, 1.0, 1.5, 0.0)
physics_flood_case("outflow_exceeds_inflow", 100.0, 1.0, 5.0, 1.0, 10.0)

rows["physics_flood"] = physics_cases
physics_cases = []

FIRE_ALPHA = {
    "slow": Decimal("0.00293"), "medium": Decimal("0.0117"),
    "fast": Decimal("0.0469"), "ultrafast": Decimal("0.1876"),
}

def physics_fire_hrr_case(name, growth_class, time_s):
    alpha = FIRE_ALPHA[growth_class]
    hrr = alpha * Decimal(time_s) * Decimal(time_s)
    physics_cases.append({
        "kind": "fire_hrr", "name": name,
        "alpha_kw_per_s2": alpha, "time_s": Decimal(time_s), "result": hrr,
    })

physics_fire_hrr_case("worked_example_medium_120s", "medium", 120.0)
physics_fire_hrr_case("ultrafast_300s", "ultrafast", 300.0)
physics_fire_hrr_case("zero_time", "slow", 0.0)

rows["physics_fire_hrr"] = physics_cases
physics_cases = []

def plume_concentration(Q, u, sigma_y, sigma_z, y, z, H):
    Q = Decimal(Q); u = Decimal(u); sigma_y = Decimal(sigma_y); sigma_z = Decimal(sigma_z)
    y = Decimal(y); z = Decimal(z); H = Decimal(H)
    horiz = (-(y * y) / (2 * sigma_y * sigma_y)).exp()
    below = z - H
    above = z + H
    vertical = (-(below * below) / (2 * sigma_z * sigma_z)).exp() \
             + (-(above * above) / (2 * sigma_z * sigma_z)).exp()
    denom = 2 * Decimal(repr(math.pi)) * u * sigma_y * sigma_z
    return (Q / denom) * horiz * vertical

def physics_plume_case(name, Q, u, sigma_y, sigma_z, y, z, H):
    conc = plume_concentration(Q, u, sigma_y, sigma_z, y, z, H)
    physics_cases.append({
        "kind": "plume_concentration", "name": name,
        "emission_rate": Q, "wind_speed_m_s": u,
        "sigma_y_m": sigma_y, "sigma_z_m": sigma_z,
        "crosswind_offset_m": y, "receptor_height_m": z,
        "effective_source_height_m": H, "result": conc,
    })

physics_plume_case("worked_example_ground_centerline", 10.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0)
physics_plume_case("off_axis", 10.0, 2.0, 50.0, 20.0, 100.0, 0.0, 0.0)
physics_plume_case("zero_emission", 0.0, 2.0, 50.0, 20.0, 0.0, 0.0, 0.0)

rows["physics_plume"] = physics_cases
physics_cases = []

def greenshields_speed(v_free, k_jam, k):
    v_free = Decimal(v_free); k_jam = Decimal(k_jam); k = Decimal(k)
    return v_free * (1 - k / k_jam)

def physics_traffic_case(name, v_free, k_jam, k):
    speed = greenshields_speed(v_free, k_jam, k)
    flow = Decimal(k) * speed
    physics_cases.append({
        "kind": "traffic_greenshields", "name": name,
        "free_flow_speed": v_free, "jam_density": k_jam, "density": k,
        "speed_result": speed, "flow_result": flow,
    })

physics_traffic_case("worked_example_half_jam", 30.0, 120.0, 60.0)
physics_traffic_case("worked_example_quarter_jam", 30.0, 120.0, 30.0)
physics_traffic_case("zero_density", 30.0, 120.0, 0.0)
physics_traffic_case("at_jam_density", 30.0, 120.0, 120.0)
physics_traffic_case("beyond_jam_density", 30.0, 120.0, 150.0)

rows["physics_traffic"] = physics_cases

# --------------------------------------------------------------- write ---
out_path = pathlib.Path(__file__).resolve().parent / "golden.csv"
with open(out_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["# City Brain core golden test vectors"])
    w.writerow(["# Generated independently in Python via gen_vectors.py."])
    w.writerow(["# Do not hand-edit; regenerate from gen_vectors.py."])
    for section, data in rows.items():
        w.writerow([f"#SECTION:{section}"])
        if not data:
            continue
        fieldnames = list(data[0].keys())
        w.writerow(fieldnames)
        for row in data:
            w.writerow([row[k] for k in fieldnames])
        w.writerow([])

print(f"wrote {sum(len(v) for v in rows.values())} total cases across {len(rows)} sections to {out_path}")
for k, v in rows.items():
    print(f"  {k}: {len(v)} cases")
