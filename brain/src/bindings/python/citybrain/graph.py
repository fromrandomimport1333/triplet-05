"""
Wrapper for citybrain/graph.h: a weighted directed graph over categories
with odds multipliers bounded to [m_min, m_max] under any composition.
Nodes are plain integer indices. See citybrain.example_profile for a
worked sample edge set.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Sequence, Tuple

from citybrain._raw import ffi, lib
from citybrain.errors import check

MAX_NODES = 8


class EdgeKind(IntEnum):
    NONE = 0
    RAISES = 1
    LOWERS = 2


@dataclass(frozen=True)
class GraphConfig:
    m_min: float = 0.5
    m_max: float = 3.0
    n_nodes: int = 4


class Graph:
    """A correlation graph. Owns a C cb_graph_t internally."""

    def __init__(self, config: GraphConfig):
        self._config = config
        self._graph = ffi.new("cb_graph_t *")
        c_cfg = ffi.new("cb_graph_config_t *")
        c_cfg.m_min = config.m_min
        c_cfg.m_max = config.m_max
        c_cfg.n_nodes = config.n_nodes
        check(lib.cb_graph_init(self._graph, c_cfg))

    def set_edge(self, from_node: int, to_node: int, kind: EdgeKind, multiplier: float) -> None:
        """Set one directed edge; multiplier is clipped into [m_min, m_max]."""
        check(lib.cb_graph_set_edge(self._graph, from_node, to_node, int(kind), multiplier))

    def get_edge(self, from_node: int, to_node: int) -> Tuple[EdgeKind, float]:
        kind_out = ffi.new("cb_edge_kind_t *")
        m_out = ffi.new("double *")
        check(lib.cb_graph_get_edge(self._graph, from_node, to_node, kind_out, m_out))
        return (EdgeKind(kind_out[0]), m_out[0])

    def clear_edge(self, from_node: int, to_node: int) -> None:
        check(lib.cb_graph_clear_edge(self._graph, from_node, to_node))

    def path_multiplier(self, path: Sequence[int]) -> float:
        """Compose the multipliers along path and clip the product to [m_min, m_max]."""
        c_path = ffi.new("int32_t[]", list(path)) if path else ffi.NULL
        out = ffi.new("double *")
        check(lib.cb_graph_path_multiplier(self._graph, c_path, len(path), out))
        return out[0]

    def extremal_multiplier(self, from_node: int, to_node: int) -> Tuple[float, float]:
        """(min, max) composed multiplier over all simple paths. Raises
        NotFoundError if no path exists."""
        lo = ffi.new("double *")
        hi = ffi.new("double *")
        check(lib.cb_graph_extremal_multiplier(self._graph, from_node, to_node, lo, hi))
        return (lo[0], hi[0])

    def adjust_prior(self, target: int, active: Sequence[int], prior: float) -> Tuple[float, float]:
        """(adjusted_prior, multiplier) for prior on target given active categories."""
        c_active = ffi.new("int32_t[]", list(active)) if active else ffi.NULL
        adj = ffi.new("double *")
        mult = ffi.new("double *")
        check(lib.cb_graph_adjust_prior(self._graph, target, c_active, len(active),
                                         prior, adj, mult))
        return (adj[0], mult[0])

    def has_unrelated_pair(self, active: Sequence[int]) -> bool:
        """True if active has two categories with no edge between them either way."""
        c_active = ffi.new("int32_t[]", list(active)) if active else ffi.NULL
        out = ffi.new("int32_t *")
        check(lib.cb_graph_has_unrelated_pair(self._graph, c_active, len(active), out))
        return bool(out[0])


def default_config() -> GraphConfig:
    c = ffi.new("cb_graph_config_t *")
    check(lib.cb_graph_config_init(c))
    return GraphConfig(m_min=c.m_min, m_max=c.m_max, n_nodes=c.n_nodes)
