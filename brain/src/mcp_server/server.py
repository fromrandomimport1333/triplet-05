"""
citybrain MCP server.

Read-only tools over the citybrain library: evidence fusion, change
detection, correlation-graph queries, decision evaluation, and the
physics module. Each tool validates its input, calls one citybrain
function, and returns the result as JSON -- no math is duplicated here.
No tool can dispatch or call an actuator; that's not a policy, it's just
that no such function exists in this file.

Run: python mcp_server/server.py, or `citybrain-mcp` once installed.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

try:
    from mcp.server.mcpserver import MCPServer as _ServerClass
except ImportError:  # mcp 1.x name
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore[no-redef]

try:
    from mcp.types import ToolAnnotations
    _READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=False)
except ImportError:  # pragma: no cover - older SDK without ToolAnnotations
    _READ_ONLY = None

import citybrain

mcp = _ServerClass(
    name="citybrain",
    title="City Brain deterministic math core",
    instructions=(
        "Read-only analysis tools over the citybrain decision library: "
        "evidence fusion, change-point detection, correlation-graph "
        "queries, decision evaluation, and physical likelihood models. "
        "No tool dispatches or actuates anything."
    ),
)


def _tool_kwargs(description: str) -> dict:
    kwargs: dict[str, Any] = {"description": description}
    if _READ_ONLY is not None:
        kwargs["annotations"] = _READ_ONLY
    return kwargs


@mcp.tool(**_tool_kwargs(
    "Fuse weighted evidence items into a posterior probability. Read-only."
))
def fuse_evidence(
    items: list[dict],
    lr_cap: float = 8.0,
    prior: float = 0.02,
    posterior_floor: float = 0.0,
    posterior_ceiling: float = 1.0,
    group_cap_enabled: bool = True,
) -> dict:
    """items: list of {likelihood_ratio, weight, group_id}."""
    cfg = citybrain.fusion.FusionConfig(
        lr_cap=lr_cap, prior=prior,
        posterior_floor=posterior_floor, posterior_ceiling=posterior_ceiling,
        group_cap_enabled=group_cap_enabled,
    )
    evidence = [
        citybrain.fusion.Evidence(
            likelihood_ratio=float(it["likelihood_ratio"]),
            weight=float(it["weight"]),
            group_id=int(it.get("group_id", 0)),
        )
        for it in items
    ]
    result = citybrain.fusion.fuse(cfg, evidence)
    return {
        "posterior": result.posterior,
        "prior_log_odds": result.prior_log_odds,
        "evidence_log_odds": result.evidence_log_odds,
        "posterior_log_odds": result.posterior_log_odds,
        "n_items": result.n_items,
        "n_groups_used": result.n_groups_used,
        "n_items_clamped": result.n_items_clamped,
        "n_groups_clamped": result.n_groups_clamped,
    }


@mcp.tool(**_tool_kwargs(
    "Minimum number of independent capped evidence groups needed to reach "
    "an action threshold, and the posterior reachable with that many. Read-only."
))
def fault_tolerance_bound(
    lr_cap: float = 8.0,
    prior: float = 0.02,
    threshold: float = 0.20,
    max_weight: float = 1.0,
    odds_multiplier: float = 1.0,
) -> dict:
    params = citybrain.fusion.FaultToleranceParams(
        lr_cap=lr_cap, prior=prior, threshold=threshold,
        max_weight=max_weight, odds_multiplier=odds_multiplier,
    )
    n_min = citybrain.fusion.min_sources(params)
    return {
        "n_min_sources": n_min,
        "posterior_at_n_min": citybrain.fusion.max_posterior(params, n_min),
        "posterior_at_n_min_minus_1": (
            citybrain.fusion.max_posterior(params, n_min - 1) if n_min > 0 else None
        ),
    }


@mcp.tool(**_tool_kwargs(
    "Run the EWMA+CUSUM change-point detector over a list of measurements "
    "and report the alarm state after each one. Read-only, no stored state "
    "between calls. Answers whether the stream shows a sustained shift, "
    "not whether it can be trusted."
))
def detect_change_points(
    values: list[float],
    ewma_alpha: float = 0.10,
    var_alpha: float = 0.05,
    cusum_slack: float = 0.5,
    cusum_threshold: float = 5.0,
    warmup_samples: int = 20,
) -> dict:
    if not values:
        return {"error": "values must be non-empty"}
    cfg = citybrain.detector.DetectorConfig(
        ewma_alpha=ewma_alpha, var_alpha=var_alpha,
        cusum_slack=cusum_slack, cusum_threshold=cusum_threshold,
        warmup_samples=warmup_samples,
    )
    det = citybrain.detector.Detector(cfg, first_value=values[0], initial_sigma=1.0)
    outputs = []
    for v in values:
        out = det.update(v)
        outputs.append({
            "value": v,
            "baseline": out.baseline,
            "normalised_residual": out.normalised_residual,
            "cusum_high": out.cusum_high,
            "cusum_low": out.cusum_low,
            "shift_up": out.shift_up,
            "shift_down": out.shift_down,
            "warmup": out.warmup,
        })
    first_alarm = next(
        (i for i, o in enumerate(outputs) if o["shift_up"] or o["shift_down"]), None
    )
    return {"samples": outputs, "first_alarm_index": first_alarm}


@mcp.tool(**_tool_kwargs(
    "Adjust a prior for a target category given active categories, using "
    "the bundled example correlation graph. Read-only."
))
def graph_adjust_prior(
    target_category: int,
    active_categories: list[int],
    prior: float,
    m_min: float = 0.5,
    m_max: float = 3.0,
) -> dict:
    cfg = citybrain.graph.GraphConfig(m_min=m_min, m_max=m_max, n_nodes=4)
    g = citybrain.graph.Graph(cfg)
    citybrain.example_profile.load_example_graph(g)
    adjusted, multiplier = g.adjust_prior(target_category, active_categories, prior)
    return {"adjusted_prior": adjusted, "applied_multiplier": multiplier}


@mcp.tool(**_tool_kwargs(
    "Check whether a set of active categories has a pair with no modelled "
    "relationship in the example graph. Read-only, structural query only."
))
def graph_has_unrelated_pair(
    active_categories: list[int],
    m_min: float = 0.5,
    m_max: float = 3.0,
) -> dict:
    cfg = citybrain.graph.GraphConfig(m_min=m_min, m_max=m_max, n_nodes=4)
    g = citybrain.graph.Graph(cfg)
    citybrain.example_profile.load_example_graph(g)
    return {"has_unrelated_pair": g.has_unrelated_pair(active_categories)}


@mcp.tool(**_tool_kwargs(
    "Evaluate the expected-cost decision rule for a posterior and a cost "
    "table, returning the lower-cost action. Read-only -- returns a value "
    "only, never dispatches anything."
))
def evaluate_decision(
    posterior: float,
    cost_act_when_false: float = 1.0,
    cost_hold_when_true: float = 20.0,
    cost_act_when_true: float = 0.0,
    cost_hold_when_false: float = 0.0,
) -> dict:
    table = citybrain.decision.CostTable(
        cost_act_when_false=cost_act_when_false,
        cost_hold_when_true=cost_hold_when_true,
        cost_act_when_true=cost_act_when_true,
        cost_hold_when_false=cost_hold_when_false,
    )
    result = citybrain.decision.expected_cost(table, posterior)
    return {
        "action": result.action.name,
        "expected_cost_act": result.expected_cost_act,
        "expected_cost_hold": result.expected_cost_hold,
        "indifference_threshold": result.indifference_threshold,
        "margin": result.margin,
    }


# Physics tools: each answers how consistent a reading is with an
# incident, as a likelihood ratio -- never a verdict on the reading itself.

@mcp.tool(**_tool_kwargs(
    "Gaussian likelihood-ratio test between two hypothesised means with a "
    "shared noise scale. Read-only."
))
def physics_gaussian_likelihood_ratio(
    observed: float,
    expected_h1: float,
    expected_h0: float,
    sigma: float,
) -> dict:
    return {"likelihood_ratio": citybrain.physics.gaussian_likelihood_ratio(
        observed, expected_h1, expected_h0, sigma)}


@mcp.tool(**_tool_kwargs(
    "Expected water-level rise rate for a shallow reservoir model. Read-only."
))
def physics_flood_expected_rise_rate(
    area_m2: float,
    inflow_m3_per_s: float,
    drainage_coefficient: float,
    drainage_exponent: float,
    water_level_m: float,
) -> dict:
    return {"expected_rate_m_per_s": citybrain.physics.flood_rise_rate(
        area_m2, inflow_m3_per_s, drainage_coefficient, drainage_exponent,
        water_level_m)}


@mcp.tool(**_tool_kwargs(
    "Published NFPA 72 t-squared fire growth coefficient for a growth "
    "class (slow/medium/fast/ultrafast). Read-only."
))
def physics_fire_growth_class_alpha(growth_class: str) -> dict:
    mapping = {
        "slow": citybrain.physics.FireGrowthClass.SLOW,
        "medium": citybrain.physics.FireGrowthClass.MEDIUM,
        "fast": citybrain.physics.FireGrowthClass.FAST,
        "ultrafast": citybrain.physics.FireGrowthClass.ULTRAFAST,
    }
    key = growth_class.strip().lower()
    if key not in mapping:
        return {"error": f"growth_class must be one of {sorted(mapping)}"}
    return {"alpha_kw_per_s2": citybrain.physics.fire_growth_class_alpha(mapping[key])}


@mcp.tool(**_tool_kwargs(
    "t-squared fire heat release rate: Q(t) = alpha * t^2 (kW). Read-only."
))
def physics_fire_heat_release_rate(alpha_kw_per_s2: float, time_s: float) -> dict:
    return {"hrr_kw": citybrain.physics.fire_heat_release_rate(alpha_kw_per_s2, time_s)}


@mcp.tool(**_tool_kwargs(
    "Gaussian plume concentration at a receptor downwind of a source. Read-only."
))
def physics_plume_concentration(
    emission_rate: float,
    wind_speed_m_s: float,
    sigma_y_m: float,
    sigma_z_m: float,
    crosswind_offset_m: float,
    receptor_height_m: float,
    effective_source_height_m: float,
) -> dict:
    return {"concentration": citybrain.physics.plume_concentration(
        emission_rate, wind_speed_m_s, sigma_y_m, sigma_z_m,
        crosswind_offset_m, receptor_height_m, effective_source_height_m)}


@mcp.tool(**_tool_kwargs(
    "Greenshields expected speed for a given traffic density. Read-only."
))
def physics_traffic_expected_speed(
    free_flow_speed: float,
    jam_density: float,
    density: float,
) -> dict:
    return {"expected_speed": citybrain.physics.traffic_greenshields_speed(
        free_flow_speed, jam_density, density)}


@mcp.tool(**_tool_kwargs(
    "Greenshields fundamental-diagram flow: q = k * v(k). Read-only."
))
def physics_traffic_expected_flow(
    free_flow_speed: float,
    jam_density: float,
    density: float,
) -> dict:
    return {"expected_flow": citybrain.physics.traffic_greenshields_flow(
        free_flow_speed, jam_density, density)}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
