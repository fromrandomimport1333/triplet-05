"""
citybrain: a deterministic decision library. Evidence fusion, change-point
detection, bounded multiplier graphs, expected-cost decisions, optimal
assignment, cache-key algebra, bounded calibration, a tabular MDP solver,
and an optional physics module -- one Python module per C module.

    import citybrain
    citybrain.fusion.fuse(...)
    citybrain.detector.Detector(...)
    citybrain.graph.Graph(...)
"""
from citybrain import (
    assignment,
    cache,
    calibration,
    common,
    decision,
    detector,
    errors,
    example_profile,
    fusion,
    graph,
    mdp,
    physics,
)
from citybrain.errors import CityBrainError

__version__ = common.version_string()

__all__ = [
    "assignment", "cache", "calibration", "common", "decision", "detector",
    "errors", "example_profile", "fusion", "graph", "mdp", "physics",
    "CityBrainError", "__version__",
]
