"""
Direct unit tests for the Python wrapper layer itself: exception mapping,
type conversions, and the handful of behaviors that are specific to the
Python binding rather than already covered by the golden vectors (which only
check numerical agreement with C, not e.g. that the right Python exception
subclass is raised).
"""
import math

import pytest

import citybrain
from citybrain import errors


def test_exception_hierarchy_subclasses_citybrain_error():
    for exc_cls in (
        errors.NullArgumentError, errors.DomainError, errors.NotFiniteError,
        errors.CapacityError, errors.StateError, errors.NotFoundError,
        errors.NoSolutionError, errors.NoConvergenceError, errors.UnsupportedError,
    ):
        assert issubclass(exc_cls, errors.CityBrainError)
        assert issubclass(exc_cls, Exception)


def test_fusion_domain_error_is_specific_subclass():
    cfg = citybrain.fusion.default_config()
    with pytest.raises(errors.DomainError):
        citybrain.fusion.fuse_independent(cfg, [citybrain.fusion.Evidence(0.0, 1.0, 0)])


def test_fusion_not_finite_error():
    cfg = citybrain.fusion.default_config()
    with pytest.raises(errors.NotFiniteError):
        citybrain.fusion.fuse_independent(cfg, [citybrain.fusion.Evidence(float("nan"), 1.0, 0)])


def test_fusion_weight_above_one_rejected():
    """The adversarial scenario that matters most for this binding: a
    compromised caller handing maximum weight to fabricated evidence must
    still be rejected by validation, not silently accepted."""
    cfg = citybrain.fusion.default_config()
    with pytest.raises(errors.DomainError):
        citybrain.fusion.fuse_independent(cfg, [citybrain.fusion.Evidence(8.0, 100.0, 0)])


def test_worked_example_matches_brief():
    cfg = citybrain.fusion.FusionConfig(lr_cap=8.0, prior=0.02)
    one = citybrain.fusion.fuse_independent(cfg, [citybrain.fusion.Evidence(1e300, 1.0, 0)])
    assert math.isclose(one.posterior, 0.1403508771929825, rel_tol=1e-12)
    two = citybrain.fusion.fuse_independent(
        cfg, [citybrain.fusion.Evidence(1e300, 1.0, 0), citybrain.fusion.Evidence(1e300, 1.0, 1)]
    )
    assert math.isclose(two.posterior, 0.5663716814159292, rel_tol=1e-12)
    assert one.posterior < 0.20 <= two.posterior


def test_detector_lifecycle():
    cfg = citybrain.detector.default_config()
    det = citybrain.detector.Detector(cfg, first_value=0.0, initial_sigma=1.0)
    out = det.update(0.0)
    assert out.warmup is True
    assert out.shift_up is False
    b, s = det.baseline()
    assert isinstance(b, float) and isinstance(s, float)
    det.reset_alarms()  # must not raise


def test_graph_floor_holds_under_composition():
    cfg = citybrain.graph.GraphConfig(m_min=0.5, m_max=3.0, n_nodes=8)
    g = citybrain.graph.Graph(cfg)
    for i in range(7):
        g.set_edge(i, i + 1, citybrain.graph.EdgeKind.LOWERS, 0.5)
    m = g.path_multiplier(list(range(8)))
    assert m >= cfg.m_min
    assert math.isclose(m, cfg.m_min, rel_tol=1e-12)


def test_graph_unrelated_pair():
    cfg = citybrain.graph.default_config()
    g = citybrain.graph.Graph(cfg)
    citybrain.example_profile.load_example_graph(g)
    flood = citybrain.example_profile.ExampleCategory.FLOOD
    fire = citybrain.example_profile.ExampleCategory.FIRE
    accident = citybrain.example_profile.ExampleCategory.ACCIDENT
    assert g.has_unrelated_pair([flood, fire]) is False
    assert g.has_unrelated_pair([flood, fire, accident]) is True


def test_assignment_rectangular_and_errors():
    assign, total = citybrain.assignment.solve([[9, 1, 8, 7], [6, 2, 3, 9]])
    assert len(assign) == 2
    assert total == 4.0

    with pytest.raises(errors.NoSolutionError):
        citybrain.assignment.solve([[1000, 1000], [1, 2]], infeasible_at=999.0)


def test_decision_tie_goes_to_hold():
    table = citybrain.decision.default_cost_table()
    result = citybrain.decision.expected_cost(table, 1.0 / 21.0)
    assert result.action == citybrain.decision.Action.HOLD


def test_calibration_never_escapes_bounds():
    bounds = citybrain.calibration.default_bounds()
    theta = 0.5
    for _ in range(200):
        result = citybrain.calibration.step(bounds, theta, 1e12)
        theta = result.value
        assert bounds.lo <= theta <= bounds.hi
    assert theta == bounds.hi


def test_cache_key_hash_stable():
    k = citybrain.cache.make_key(0, 0, 0, 0)
    assert k.hash == 901300984310592933


def test_mdp_two_state():
    transition = [
        [[0.9, 0.1], [0.9, 0.1]],
        [[0.1, 0.9], [0.95, 0.05]],
    ]
    reward = [[0.0, -1.0], [-5.0, -1.0]]
    solution = citybrain.mdp.solve(transition, reward)
    assert solution.policy[0] == 0  # hold when quiet
    assert solution.policy[1] == 1  # act when active
    assert solution.converged is True


def test_mdp_state_index_roundtrip():
    dims = [4, 4, 8]
    for t in range(4):
        for s in range(4):
            for z in range(8):
                idx = citybrain.mdp.state_index(dims, [t, s, z])
                assert citybrain.mdp.state_decode(dims, idx) == [t, s, z]
    assert citybrain.mdp.state_count(dims) == 4 * 4 * 8


def test_version():
    assert citybrain.__version__ == "1.1.0"
    assert citybrain.common.version_numbers() == (1, 1, 0)


def test_physics_fire_growth_classes_ordered():
    s = citybrain.physics.fire_growth_class_alpha(citybrain.physics.FireGrowthClass.SLOW)
    m = citybrain.physics.fire_growth_class_alpha(citybrain.physics.FireGrowthClass.MEDIUM)
    f = citybrain.physics.fire_growth_class_alpha(citybrain.physics.FireGrowthClass.FAST)
    u = citybrain.physics.fire_growth_class_alpha(citybrain.physics.FireGrowthClass.ULTRAFAST)
    assert s < m < f < u
    # Published NFPA 72 / SFPE values (Karlsson & Quintiere, 2000, table 1.1).
    assert math.isclose(s, 0.00293, rel_tol=1e-9)
    assert math.isclose(u, 0.1876, rel_tol=1e-9)


def test_physics_feeds_fusion_unmodified():
    """The pluggable-module claim, checked in code: a physics-derived
    likelihood ratio is ordinary fusion input, with no special path and no
    exemption from the fault-tolerance cap."""
    rate = citybrain.physics.flood_rise_rate(200.0, 12.0, 0.5, 1.5, 1.0)
    lr = citybrain.physics.gaussian_likelihood_ratio(rate, rate, 0.0, 0.01)

    cfg = citybrain.fusion.default_config()
    result = citybrain.fusion.fuse_independent(
        cfg, [citybrain.fusion.Evidence(lr, 1.0, 0)]
    )
    assert result.posterior > cfg.prior
    assert 0.0 <= result.posterior <= 1.0
    assert result.evidence_log_odds <= math.log(cfg.lr_cap) + 1e-9


def test_physics_absurd_readings_are_low_evidence_not_errors():
    """Scope boundary, checked in code: an absurd reading never raises, and
    the result is evidentiary weight (a number), never a validity verdict.
    Mirrors the C adversarial scenarios in tests/test_adversarial.c."""
    # Flood: an impossible instantaneous drop versus a flood-rise hypothesis.
    expected_rate = citybrain.physics.flood_rise_rate(500.0, 50.0, 0.2, 1.5, 1.0)
    lr = citybrain.physics.gaussian_likelihood_ratio(-500.0, expected_rate, 0.0, 0.05)
    assert lr >= 0.0 and not math.isnan(lr)
    assert lr < 1e-6

    # Traffic: an impossibly high speed versus a near-jam congestion hypothesis.
    expected_jam_speed = citybrain.physics.traffic_greenshields_speed(30.0, 120.0, 110.0)
    lr = citybrain.physics.gaussian_likelihood_ratio(300.0, expected_jam_speed, 30.0, 1.0)
    assert lr >= 0.0 and not math.isnan(lr)
    assert lr < 1e-6

    # Fire: clean air despite a severe, close-range fire hypothesis.
    alpha = citybrain.physics.fire_growth_class_alpha(citybrain.physics.FireGrowthClass.ULTRAFAST)
    hrr = citybrain.physics.fire_heat_release_rate(alpha, 300.0)
    expected_conc = citybrain.physics.plume_concentration(0.1 * hrr, 2.0, 20.0, 10.0, 0.0, 0.0, 0.0)
    lr = citybrain.physics.gaussian_likelihood_ratio(0.0, expected_conc, 0.0, expected_conc * 0.2)
    assert lr >= 0.0 and not math.isnan(lr)
    assert lr < 1e-4


def test_physics_domain_errors_not_trust_judgements():
    with pytest.raises(citybrain.errors.DomainError):
        citybrain.physics.flood_rise_rate(100.0, 1.0, 0.5, 1.5, -1.0)
    with pytest.raises(citybrain.errors.DomainError):
        citybrain.physics.plume_concentration(10.0, 0.0, 50.0, 20.0, 0.0, 0.0, 0.0)
    with pytest.raises(citybrain.errors.DomainError):
        citybrain.physics.gaussian_likelihood_ratio(1.0, 2.0, 0.0, 0.0)
    with pytest.raises(citybrain.errors.DomainError):
        citybrain.physics.traffic_greenshields_speed(0.0, 120.0, 30.0)
    with pytest.raises(citybrain.errors.NotFiniteError):
        citybrain.physics.fire_heat_release_rate(float("nan"), 10.0)


def test_physics_traffic_beyond_jam_density_is_evaluated_not_rejected():
    """Documents, in code, that density beyond jam_density is computed as
    given rather than special-cased -- the same point made in the C header
    and tests/test_physics.c."""
    speed = citybrain.physics.traffic_greenshields_speed(30.0, 120.0, 150.0)
    assert speed < 0.0
    assert math.isclose(speed, 30.0 * (1.0 - 150.0 / 120.0), rel_tol=1e-12)
