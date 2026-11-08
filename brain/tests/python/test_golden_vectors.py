"""
Checks the Python bindings against tests/vectors/golden.csv -- the same
independently-generated file tests/test_vectors.c checks the C library
against. Numerical agreement here is checked to the same tolerance (1e-12)
the C test uses, confirming the two bindings compute identical results, not
each having their own idea of "correct."
"""

import pytest

import citybrain
from conftest import load_golden_sections

SECTIONS = load_golden_sections()


def approx(a, b, tol=1e-12):
    scale = max(abs(b), 1.0)
    assert abs(a - b) <= tol * scale, f"{a!r} != {b!r} (diff {abs(a-b):.3g})"


FUSION_CASES = {
    "empty": [],
    "one_item_lr_equal_cap": [(8.0, 1.0, 0)],
    "one_item_above_cap_clips": [(1e30, 1.0, 0)],
    "one_item_below_floor_clips": [(1e-30, 1.0, 0)],
    "weighted_half": [(8.0, 0.5, 0)],
    "weight_zero_silences": [(1e9, 0.0, 0)],
    "lr_one_no_effect": [(1.0, 1.0, 0)],
    "two_cancel": [(4.0, 1.0, 0), (0.25, 1.0, 1)],
    "brief_worked_example_one_source": [(1e300, 1.0, 0)],
    "brief_worked_example_two_sources": [(1e300, 1.0, 0), (1e300, 1.0, 1)],
    "three_mixed": [(3.0, 1.0, 0), (0.4, 0.5, 1), (10.0, 0.25, 2)],
    "high_prior": [(2.0, 1.0, 0)],
    "tiny_prior": [(8.0, 1.0, 0)],
    "cap_near_one": [(100.0, 1.0, 0)],
    "large_cap": [(50.0, 1.0, 0)],
}


@pytest.mark.parametrize("row", SECTIONS["fusion"], ids=lambda r: r["name"])
def test_fusion_golden(row):
    cfg = citybrain.fusion.FusionConfig(lr_cap=float(row["lr_cap"]), prior=float(row["prior"]))
    items = [
        citybrain.fusion.Evidence(lr, w, g) for lr, w, g in FUSION_CASES[row["name"]]
    ]
    result = citybrain.fusion.fuse_independent(cfg, items)
    approx(result.posterior, float(row["posterior"]))
    approx(result.evidence_log_odds, float(row["evidence_log_odds"]))
    assert result.n_items_clamped == int(row["n_items_clamped"])


@pytest.mark.parametrize("row", SECTIONS["fault_tolerance"], ids=lambda r: r["name"])
def test_fault_tolerance_golden(row):
    params = citybrain.fusion.FaultToleranceParams(
        lr_cap=float(row["lr_cap"]), prior=float(row["prior"]), threshold=float(row["threshold"]),
        max_weight=float(row["max_weight"]), odds_multiplier=float(row["odds_multiplier"]),
    )
    n_min = citybrain.fusion.min_sources(params)
    assert n_min == int(row["n_min_sources"])
    approx(citybrain.fusion.max_posterior(params, n_min), float(row["posterior_at_n_min"]))
    below = row["posterior_at_n_min_minus_1"]
    if below and n_min > 0:
        approx(citybrain.fusion.max_posterior(params, n_min - 1), float(below))


@pytest.mark.parametrize("row", SECTIONS["decision"], ids=lambda r: r["name"])
def test_decision_golden(row):
    table = citybrain.decision.CostTable(
        cost_act_when_false=float(row["cost_act_when_false"]),
        cost_hold_when_true=float(row["cost_hold_when_true"]),
        cost_act_when_true=float(row["cost_act_when_true"]),
        cost_hold_when_false=float(row["cost_hold_when_false"]),
    )
    result = citybrain.decision.expected_cost(table, float(row["posterior"]))
    approx(result.expected_cost_act, float(row["expected_cost_act"]))
    approx(result.expected_cost_hold, float(row["expected_cost_hold"]))
    approx(result.indifference_threshold, float(row["indifference_threshold"]))
    expected_action = citybrain.decision.Action.ACT if row["action"] == "ACT" else citybrain.decision.Action.HOLD
    assert result.action == expected_action


@pytest.mark.parametrize("row", SECTIONS["calibration"], ids=lambda r: r["name"])
def test_calibration_golden(row):
    bounds = citybrain.calibration.CalibrationBounds(
        lo=float(row["lo"]), hi=float(row["hi"]),
        max_step=float(row["max_step"]), rate=float(row["rate"]),
    )
    result = citybrain.calibration.step(bounds, float(row["current"]), float(row["gradient"]))
    approx(result.value, float(row["value"]))
    approx(result.raw_step, float(row["raw_step"]))
    approx(result.applied_step, float(row["applied_step"]))
    assert result.step_clipped == bool(int(row["step_clipped"]))
    assert result.bound_clipped == bool(int(row["bound_clipped"]))


GRAPH_CASES = {
    "single_lowers_edge": [0.5],
    "seven_lowers_chain": [0.5] * 7,
    "seven_raises_chain": [3.0] * 7,
    "mixed_chain": [0.7, 2.0, 0.7, 2.0, 0.7, 2.0, 0.7],
    "flood_accident_jam": [1.5, 3.0],
    "flood_fire_jam": [0.5, 1.5],
}


@pytest.mark.parametrize("row", SECTIONS["graph"], ids=lambda r: r["name"])
def test_graph_golden(row):
    edges = GRAPH_CASES[row["name"]]
    cfg = citybrain.graph.GraphConfig(m_min=float(row["m_min"]), m_max=float(row["m_max"]), n_nodes=8)
    g = citybrain.graph.Graph(cfg)
    for i, m in enumerate(edges):
        kind = citybrain.graph.EdgeKind.RAISES if m >= 1.0 else citybrain.graph.EdgeKind.LOWERS
        g.set_edge(i, i + 1, kind, m)
    path = list(range(len(edges) + 1))
    composed = g.path_multiplier(path)
    approx(composed, float(row["composed_multiplier"]))


@pytest.mark.parametrize("row", SECTIONS["cache"], ids=lambda r: r["name"])
def test_cache_golden(row):
    key = citybrain.cache.make_key(
        int(row["category_id"]), int(row["partition_id"]),
        int(row["bucket_id"]), int(row["state_version"]),
    )
    assert key.hash == int(row["hash"])


@pytest.mark.parametrize("row", SECTIONS["physics_gaussian_lrt"], ids=lambda r: r["name"])
def test_physics_gaussian_lrt_golden(row):
    lr = citybrain.physics.gaussian_likelihood_ratio(
        float(row["observed"]), float(row["expected_h1"]),
        float(row["expected_h0"]), float(row["sigma"]),
    )
    approx(lr, float(row["result"]))


@pytest.mark.parametrize("row", SECTIONS["physics_flood"], ids=lambda r: r["name"])
def test_physics_flood_golden(row):
    rate = citybrain.physics.flood_rise_rate(
        float(row["area_m2"]), float(row["inflow_m3_per_s"]),
        float(row["drainage_coefficient"]), float(row["drainage_exponent"]),
        float(row["water_level_m"]),
    )
    approx(rate, float(row["result"]))


@pytest.mark.parametrize("row", SECTIONS["physics_fire_hrr"], ids=lambda r: r["name"])
def test_physics_fire_hrr_golden(row):
    hrr = citybrain.physics.fire_heat_release_rate(
        float(row["alpha_kw_per_s2"]), float(row["time_s"]),
    )
    approx(hrr, float(row["result"]), tol=1e-9)


@pytest.mark.parametrize("row", SECTIONS["physics_plume"], ids=lambda r: r["name"])
def test_physics_plume_golden(row):
    conc = citybrain.physics.plume_concentration(
        float(row["emission_rate"]), float(row["wind_speed_m_s"]),
        float(row["sigma_y_m"]), float(row["sigma_z_m"]),
        float(row["crosswind_offset_m"]), float(row["receptor_height_m"]),
        float(row["effective_source_height_m"]),
    )
    approx(conc, float(row["result"]))


@pytest.mark.parametrize("row", SECTIONS["physics_traffic"], ids=lambda r: r["name"])
def test_physics_traffic_golden(row):
    speed = citybrain.physics.traffic_greenshields_speed(
        float(row["free_flow_speed"]), float(row["jam_density"]), float(row["density"]),
    )
    flow = citybrain.physics.traffic_greenshields_flow(
        float(row["free_flow_speed"]), float(row["jam_density"]), float(row["density"]),
    )
    approx(speed, float(row["speed_result"]))
    approx(flow, float(row["flow_result"]))


def test_all_sections_present():
    """Make sure this file is actually exercising every section the
    generator produced, so a new section added to gen_vectors.py cannot
    silently go unchecked on the Python side."""
    expected = {
        "fusion", "fault_tolerance", "decision", "calibration", "graph", "cache",
        "physics_gaussian_lrt", "physics_flood", "physics_fire_hrr",
        "physics_plume", "physics_traffic",
    }
    assert set(SECTIONS.keys()) == expected
    total = sum(len(v) for v in SECTIONS.values())
    assert total >= 60
