"""
Pythonic wrapper for citybrain/decision.h: expected-cost decision rule and
action ladder. The two actions are named generically: ACT and HOLD.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Sequence, Tuple

from citybrain._raw import ffi, lib
from citybrain.errors import check


class Action(IntEnum):
    HOLD = 0
    ACT = 1


@dataclass(frozen=True)
class CostTable:
    cost_act_when_false: float = 1.0
    cost_hold_when_true: float = 20.0
    cost_act_when_true: float = 0.0
    cost_hold_when_false: float = 0.0


@dataclass(frozen=True)
class DecisionResult:
    action: Action
    expected_cost_act: float
    expected_cost_hold: float
    indifference_threshold: float
    margin: float


def default_cost_table() -> CostTable:
    t = ffi.new("cb_cost_table_t *")
    check(lib.cb_cost_table_init(t))
    return CostTable(
        cost_act_when_false=t.cost_act_when_false,
        cost_hold_when_true=t.cost_hold_when_true,
        cost_act_when_true=t.cost_act_when_true,
        cost_hold_when_false=t.cost_hold_when_false,
    )


def expected_cost(table: CostTable, posterior: float) -> DecisionResult:
    """Pick the lower-expected-cost action. An exact tie resolves to HOLD."""
    t = ffi.new("cb_cost_table_t *")
    t.cost_act_when_false = table.cost_act_when_false
    t.cost_hold_when_true = table.cost_hold_when_true
    t.cost_act_when_true = table.cost_act_when_true
    t.cost_hold_when_false = table.cost_hold_when_false
    out = ffi.new("cb_decision_result_t *")
    check(lib.cb_decision_expected_cost(t, posterior, out))
    return DecisionResult(
        action=Action(out.action), expected_cost_act=out.expected_cost_act,
        expected_cost_hold=out.expected_cost_hold,
        indifference_threshold=out.indifference_threshold, margin=out.margin,
    )


@dataclass(frozen=True)
class ActionCost:
    action_id: int
    cost_if_true: float
    cost_if_false: float


def ladder(actions: Sequence[ActionCost], posterior: float) -> Tuple[int, float]:
    """(index, expected_cost) of the lowest-expected-cost rung. Ties go to
    the lowest index, so order the ladder most-passive-first."""
    n = len(actions)
    c_actions = ffi.new("cb_action_cost_t[]", n)
    for i, a in enumerate(actions):
        c_actions[i].action_id = a.action_id
        c_actions[i].cost_if_true = a.cost_if_true
        c_actions[i].cost_if_false = a.cost_if_false
    idx = ffi.new("size_t *")
    ec = ffi.new("double *")
    check(lib.cb_decision_ladder(c_actions, n, posterior, idx, ec))
    return (int(idx[0]), ec[0])
