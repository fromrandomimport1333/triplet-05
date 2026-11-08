"""The worked example of the report (docs/report/brain-scoring.md) stays true."""

from __future__ import annotations

import pytest

from tests.scoring import worked_example


def test_worked_example_numbers(capsys):
    _, decision, _, (rules, llm) = worked_example.run()
    assert rules.priority == pytest.approx(2.52)
    assert rules.trust == pytest.approx(0.95)
    assert [round(b.score, 3) for b in rules.breakdown] == [2.188, 0.701]
    assert [round(b.score, 3) for b in llm.breakdown] == [2.188, 0.626, 1.217]
    assert rules.score == pytest.approx(2.889) and llm.score == pytest.approx(4.031)
    assert decision["origin"] == "llm" and decision["score"] == 4.031
    assert decision["alternatives"] == [{"proposal_id": rules.proposal_id, "origin": "rules", "score": 2.889}]
    worked_example.main()
    assert "llm proposal (score 4.03) chosen over rules (2.89)" in capsys.readouterr().out
