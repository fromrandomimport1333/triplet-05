"""Score formula with numbers (config/scoring.yaml values)."""

from __future__ import annotations

import pytest

from src.common.models import ZoneDecl
from src.scoring.model import ScoringModel


@pytest.fixture
def m(scoring_cfg) -> ScoringModel:
    return ScoringModel(scoring_cfg)


def test_priority(m):
    # 0.8 x 2 (medium) x 1.5 (port, high exposure) x (1 + 30/60)
    assert m.priority(0.8, "medium", 1.5, 30) == pytest.approx(3.6)
    assert m.priority(1.0, "critical", 2.0, 0) == pytest.approx(8.0)
    assert m.priority(1.7, "low", 1.0, -5) == pytest.approx(1.0)      # clipped confidence and waiting


def test_zone_importance(m):
    assert m.zone_importance(ZoneDecl(zone_id="z", type="medical", exposure="low")) == 2.0
    assert m.zone_importance(ZoneDecl(zone_id="z", type="port", exposure="high")) == 1.5
    assert m.zone_importance(ZoneDecl(zone_id="z", type="commercial", exposure="medium")) == 1.0
    assert m.zone_importance(None) == 1.0


def test_cost_risk_and_penalties(m):
    assert m.cost(0, is_unit=True) == pytest.approx(0.2)
    assert m.cost(2, is_unit=True) == pytest.approx(0.35)            # 0.3 x 2/4 + 0.2
    assert m.cost(None, is_unit=False) == pytest.approx(0.35)        # unreachable = max hops
    assert m.cost(9, is_unit=True) == pytest.approx(0.5)             # hops clipped at max_hops
    assert m.risk("valve", "open") == 0.6
    assert m.risk("unit", "dispatch") == 0.4
    assert m.risk("traffic_signal", "priority") == 0.1
    assert m.risk("drone", "fly") == 0.3                             # default
    assert m.risk_penalty(0.6, 0.5) == pytest.approx(0.3)
    assert m.safe_penalty(0.6, safe=True, agree=False) == pytest.approx(1.5)
    assert m.safe_penalty(0.6, safe=True, agree=False, priority=2.0) == pytest.approx(3.0)
    assert m.safe_penalty(0.6, safe=True, agree=True) == 0.0
    assert m.safe_penalty(0.1, safe=True, agree=False) == 0.0        # low risk never penalised
    assert m.safe_penalty(0.6, safe=False, agree=False) == 0.0


def test_score_action_numbers(m):
    # flood, P 2.4: valve-01 open in its own zone, full trust -> 2.4 x 0.9 - 0.05 - 0
    s = m.score_action(priority=2.4, incident_type="flood", target_id="valve-01", action="open",
                       target_type="valve", is_unit=False, hops=0, trust=1.0, safe=False, agree=False)
    assert s.score == pytest.approx(2.11)
    # same with trust 0.5: risk penalty 0.6 x 0.5 = 0.3
    s = m.score_action(priority=2.4, incident_type="flood", target_id="valve-01", action="open",
                       target_type="valve", is_unit=False, hops=0, trust=0.5, safe=False, agree=False)
    assert s.score == pytest.approx(1.81)
    # safe mode, one sensor: extra 2.5 x 0.6 x 2.4 = 3.6 -> 2.16 - 0.05 - 3.6
    s = m.score_action(priority=2.4, incident_type="flood", target_id="valve-01", action="open",
                       target_type="valve", is_unit=False, hops=0, trust=1.0, safe=True, agree=False)
    assert s.score == pytest.approx(-1.49)
    # an action that does nothing for the incident type scores below 0
    s = m.score_action(priority=2.4, incident_type="accident", target_id="valve-01", action="open",
                       target_type="valve", is_unit=False, hops=0, trust=1.0, safe=False, agree=False)
    assert s.score < 0


def test_risk_level_and_trust(m):
    assert m.risk_level(0.7) == "high"
    assert m.risk_level(0.4) == "medium"
    assert m.risk_level(0.1) == "low"
    assert m.combine_trust([1.0, 0.5]) == pytest.approx(0.75)
    assert m.combine_trust([]) == 0.0
