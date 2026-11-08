"""Phase 2 placeholder: multivariate Isolation Forest detector.

Interface only — Phase 1 ships the EWMA detector. The pipeline will call
`score()` after step 4 and take min(ewma_score, iforest_score) once `enabled` is True.
"""
from __future__ import annotations

from typing import Protocol, Sequence


class AnomalyModel(Protocol):
    """Common interface for pluggable anomaly models."""

    enabled: bool

    def fit(self, device_id: str, samples: Sequence[Sequence[float]]) -> None:
        """Train the per-device model on historical feature vectors."""

    def score(self, device_id: str, features: Sequence[float]) -> float:
        """Return a trust score in [0, 1] (1 = normal)."""


class IsolationForestDetector:
    """Stub. Raises NotImplementedError until Phase 2."""

    enabled = False

    def fit(self, device_id: str, samples: Sequence[Sequence[float]]) -> None:
        """Not implemented in Phase 1."""
        raise NotImplementedError("IsolationForestDetector is planned for Phase 2")

    def score(self, device_id: str, features: Sequence[float]) -> float:
        """Not implemented in Phase 1."""
        raise NotImplementedError("IsolationForestDetector is planned for Phase 2")
