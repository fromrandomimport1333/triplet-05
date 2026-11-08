"""
Property-based tests for the Python bindings, using hypothesis. These mirror
the C property tests in tests/test_property.c (same invariants, same
parameter ranges) but are a genuinely independent check: a different
language, calling through the cffi boundary, generating its own random
inputs with its own RNG.

Run with `pytest --hypothesis-seed=<n>` to reproduce a specific failure, or
set HYPOTHESIS_PROFILE=ci for more examples (see hypothesis_profiles below).
"""
import math

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st, HealthCheck

import citybrain

import os

settings.register_profile("default", max_examples=200, deadline=None)
settings.register_profile("ci", max_examples=2000, deadline=None,
                           suppress_health_check=[HealthCheck.too_slow])
# Respect HYPOTHESIS_PROFILE / --hypothesis-profile if the caller set one;
# only fall back to our own "default" (200 examples) when they did not. A
# bare settings.load_profile("default") here would silently override the
# env var every time, which is exactly the bug this comment replaced.
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

log_ratio = st.floats(min_value=1.05, max_value=1000.0, allow_nan=False)
prob = st.floats(min_value=1e-6, max_value=0.5, allow_nan=False, exclude_min=False)
threshold = st.floats(min_value=0.01, max_value=0.99, allow_nan=False)
weight = st.floats(min_value=0.05, max_value=1.0, allow_nan=False)
mult = st.floats(min_value=0.1, max_value=10.0, allow_nan=False)


@given(cap=log_ratio, p0=prob, T=threshold, w=weight, m=mult)
def test_bound_is_tight(cap, p0, T, w, m):
    """n_min capped groups cross the threshold; n_min - 1 do not."""
    params = citybrain.fusion.FaultToleranceParams(
        lr_cap=cap, prior=p0, threshold=T, max_weight=w, odds_multiplier=m
    )
    try:
        n_min = citybrain.fusion.min_sources(params)
    except citybrain.errors.CapacityError:
        return
    if n_min < 1 or n_min > 200:
        return

    cfg = citybrain.fusion.FusionConfig(lr_cap=cap, prior=p0)
    # Fold the odds multiplier into the prior exactly as cb_ft_params does.
    adjusted_log_odds = citybrain.fusion.logit(p0) + math.log(m)
    if not (-700 < adjusted_log_odds < 700):
        return
    adjusted = citybrain.fusion.logistic(adjusted_log_odds)
    if not (0.0 < adjusted < 1.0):
        return
    cfg = citybrain.fusion.FusionConfig(lr_cap=cap, prior=adjusted)

    items = [citybrain.fusion.Evidence(1e300, w, i) for i in range(n_min)]

    below = citybrain.fusion.fuse_independent(cfg, items[: n_min - 1])
    assert below.posterior < T

    at = citybrain.fusion.fuse_independent(cfg, items[:n_min])
    assert at.posterior >= T


@given(
    cap=log_ratio, p0=prob,
    lrs=st.lists(st.floats(min_value=1e-12, max_value=1e12, allow_nan=False), min_size=0, max_size=20),
    ws=st.lists(weight, min_size=0, max_size=20),
)
def test_evidence_never_exceeds_ceiling(cap, p0, lrs, ws):
    """However fused evidence is shaped, the posterior never exceeds the
    ceiling the fault-tolerance formula predicts for that many groups."""
    n = min(len(lrs), len(ws))
    if n == 0:
        return
    cfg = citybrain.fusion.FusionConfig(lr_cap=cap, prior=p0)
    items = [citybrain.fusion.Evidence(lrs[i], ws[i], i) for i in range(n)]
    result = citybrain.fusion.fuse(cfg, items)

    ft = citybrain.fusion.FaultToleranceParams(lr_cap=cap, prior=p0, threshold=0.5, max_weight=1.0)
    ceiling = citybrain.fusion.max_posterior(ft, result.n_groups_used)
    assert result.posterior <= ceiling + 1e-9
    assert 0.0 <= result.posterior <= 1.0
    assert not math.isnan(result.posterior)


@given(
    m_min=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    m_max=st.floats(min_value=1.0, max_value=20.0, allow_nan=False),
    edges=st.lists(st.floats(min_value=1e-6, max_value=1e6, allow_nan=False), min_size=1, max_size=7),
)
def test_graph_composition_bounds(m_min, m_max, edges):
    cfg = citybrain.graph.GraphConfig(m_min=m_min, m_max=m_max, n_nodes=8)
    g = citybrain.graph.Graph(cfg)
    for i, raw in enumerate(edges):
        kind = citybrain.graph.EdgeKind.RAISES if raw >= 1.0 else citybrain.graph.EdgeKind.LOWERS
        g.set_edge(i, i + 1, kind, raw)
    composed = g.path_multiplier(list(range(len(edges) + 1)))
    assert m_min - 1e-9 <= composed <= m_max + 1e-9


@given(
    lo=st.floats(min_value=-100, max_value=100, allow_nan=False),
    span=st.floats(min_value=1e-3, max_value=100, allow_nan=False),
    max_step=st.floats(min_value=1e-6, max_value=10, allow_nan=False),
    gradient=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False),
)
@settings(max_examples=150, deadline=None)
def test_calibration_bounds_hold(lo, span, max_step, gradient):
    hi = lo + span
    bounds = citybrain.calibration.CalibrationBounds(lo=lo, hi=hi, max_step=max_step, rate=1.0)
    theta = (lo + hi) / 2.0
    for _ in range(10):
        result = citybrain.calibration.step(bounds, theta, gradient)
        assert lo - 1e-9 <= result.value <= hi + 1e-9
        assert abs(result.applied_step) <= max_step + 1e-9
        theta = result.value


@given(
    observed=st.floats(min_value=-1e8, max_value=1e8, allow_nan=False),
    h1=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    h0=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    sigma=st.floats(min_value=1e-6, max_value=1e6, allow_nan=False),
)
def test_physics_gaussian_lrt_never_crashes_or_nans(observed, h1, h0, sigma):
    """The physics module's shared Gaussian LRT never raises for any finite,
    valid-domain input, however extreme, and never returns NaN or a
    negative value -- robustness across a much wider range than the hand-
    picked adversarial cases in test_direct.py cover individually."""
    lr = citybrain.physics.gaussian_likelihood_ratio(observed, h1, h0, sigma)
    assert not math.isnan(lr)
    assert lr >= 0.0


@given(
    area=st.floats(min_value=1e-3, max_value=1e9, allow_nan=False),
    inflow=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    coeff=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    exponent=st.floats(min_value=1e-3, max_value=10.0, allow_nan=False),
    level=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)
def test_physics_flood_rate_never_crashes_or_nans(area, inflow, coeff, exponent, level):
    rate = citybrain.physics.flood_rise_rate(area, inflow, coeff, exponent, level)
    assert not math.isnan(rate)
    assert math.isfinite(rate)


@given(
    v_free=st.floats(min_value=1e-3, max_value=1e4, allow_nan=False),
    k_jam=st.floats(min_value=1e-3, max_value=1e4, allow_nan=False),
    density=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)
def test_physics_traffic_never_crashes_or_nans(v_free, k_jam, density):
    """Includes density far beyond jam_density -- the model's documented
    validity boundary -- confirming it is evaluated, not rejected."""
    speed = citybrain.physics.traffic_greenshields_speed(v_free, k_jam, density)
    flow = citybrain.physics.traffic_greenshields_flow(v_free, k_jam, density)
    assert not math.isnan(speed) and math.isfinite(speed)
    assert not math.isnan(flow) and math.isfinite(flow)
    assert math.isclose(flow, density * speed, rel_tol=1e-9, abs_tol=1e-9)
