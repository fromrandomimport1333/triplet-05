"""
Wrapper for citybrain/assignment.h: optimal bipartite assignment (Hungarian
algorithm). Takes and returns plain Python lists.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from citybrain._raw import ffi, lib
from citybrain.errors import check


def solve(
    matrix: Sequence[Sequence[float]],
    maximize: bool = False,
    infeasible_at: Optional[float] = None,
) -> "tuple[List[int], float]":
    """Solve the assignment problem for a cost/benefit matrix given as a
    list of rows. Returns (assignment, total); assignment[i] is the column
    matched to row i, or -1 if unassigned."""
    n_rows = len(matrix)
    n_cols = len(matrix[0]) if n_rows else 0
    flat = ffi.new("double[]", n_rows * n_cols) if n_rows * n_cols else ffi.NULL
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            flat[i * n_cols + j] = v

    params = ffi.new("cb_assign_params_t *")
    check(lib.cb_assign_params_init(params))
    params.matrix = flat
    params.n_rows = n_rows
    params.n_cols = n_cols
    params.maximize = 1 if maximize else 0
    if infeasible_at is not None:
        params.infeasible_at = infeasible_at

    need = ffi.new("size_t *")
    check(lib.cb_assign_scratch_bytes(n_rows, n_cols, need))
    scratch = ffi.new("char[]", need[0])

    out_assign = ffi.new("int32_t[]", n_rows)
    out_total = ffi.new("double *")
    check(lib.cb_assign_solve(params, out_assign, out_total, scratch, need[0]))

    return ([int(out_assign[i]) for i in range(n_rows)], out_total[0])
