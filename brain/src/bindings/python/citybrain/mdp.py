"""
Pythonic wrapper for citybrain/mdp.h: a tabular MDP value-iteration solver,
plus mixed-radix index helpers for a product state space.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from citybrain._raw import ffi, lib
from citybrain.errors import check


@dataclass(frozen=True)
class MDPSolution:
    policy: List[int]
    value: List[float]
    iterations: int
    converged: bool


def solve(
    transition: Sequence[Sequence[Sequence[float]]],
    reward: Sequence[Sequence[float]],
    discount: float = 0.95,
    tolerance: float = 1e-10,
    max_iterations: int = 10000,
    row_sum_tolerance: float = 1e-9,
) -> MDPSolution:
    """Value iteration over a dense tabular MDP. transition[s][a] is the
    distribution over next states; reward[s][a] is the expected immediate
    reward. MDPSolution.converged is False if the iteration limit was hit
    first, but the policy/value are still usable."""
    n_states = len(transition)
    n_actions = len(transition[0]) if n_states else 0

    flat_t = ffi.new("double[]", n_states * n_actions * n_states) if n_states else ffi.NULL
    flat_r = ffi.new("double[]", n_states * n_actions) if n_states else ffi.NULL
    for s in range(n_states):
        for a in range(n_actions):
            flat_r[s * n_actions + a] = reward[s][a]
            for s2 in range(n_states):
                flat_t[(s * n_actions + a) * n_states + s2] = transition[s][a][s2]

    problem = ffi.new("cb_mdp_problem_t *")
    check(lib.cb_mdp_problem_init(problem))
    problem.transition = flat_t
    problem.reward = flat_r
    problem.n_states = n_states
    problem.n_actions = n_actions
    problem.discount = discount
    problem.tolerance = tolerance
    problem.max_iterations = max_iterations
    problem.row_sum_tolerance = row_sum_tolerance

    need = ffi.new("size_t *")
    check(lib.cb_mdp_scratch_doubles(n_states, need))
    scratch = ffi.new("double[]", need[0])

    policy = ffi.new("int32_t[]", n_states)
    value = ffi.new("double[]", n_states)
    iterations = ffi.new("int32_t *")

    status = lib.cb_mdp_solve(problem, policy, value, scratch, need[0], iterations)
    converged = (status == 0)
    if status != 0 and status != 8:  # 8 == CB_ERR_NO_CONVERGE: still has a usable policy
        check(status)

    return MDPSolution(
        policy=[int(policy[i]) for i in range(n_states)],
        value=[float(value[i]) for i in range(n_states)],
        iterations=int(iterations[0]),
        converged=converged,
    )


def state_index(dims: Sequence[int], coords: Sequence[int]) -> int:
    """Mixed-radix index for a product state space, row-major, last
    dimension fastest."""
    c_dims = ffi.new("int32_t[]", list(dims))
    c_coords = ffi.new("int32_t[]", list(coords))
    out = ffi.new("int32_t *")
    check(lib.cb_mdp_state_index(c_dims, c_coords, len(dims), out))
    return out[0]


def state_decode(dims: Sequence[int], index: int) -> List[int]:
    c_dims = ffi.new("int32_t[]", list(dims))
    out = ffi.new("int32_t[]", len(dims))
    check(lib.cb_mdp_state_decode(c_dims, len(dims), index, out))
    return [int(out[i]) for i in range(len(dims))]


def state_count(dims: Sequence[int]) -> int:
    c_dims = ffi.new("int32_t[]", list(dims))
    out = ffi.new("int32_t *")
    check(lib.cb_mdp_state_count(c_dims, len(dims), out))
    return out[0]
