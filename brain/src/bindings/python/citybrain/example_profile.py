"""
Sample data, not part of the generic library: a worked category/edge set
for a demo graph. Nothing else in this package refers to it.
"""
from __future__ import annotations

from enum import IntEnum

from citybrain._raw import ffi, lib
from citybrain.errors import check
from citybrain.graph import Graph


class ExampleCategory(IntEnum):
    FLOOD = 0
    FIRE = 1
    ACCIDENT = 2
    JAM = 3


def load_example_graph(graph: Graph) -> None:
    """Install the example edge set on an already-initialised Graph with
    at least 4 nodes."""
    check(lib.cb_example_load_graph(graph._graph))
