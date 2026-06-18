"""Scoring abstractions.

A `Scorer` turns one signal (keywords, authors, embedding similarity) into a
relevance score in [0, 1]. `CombinedScorer` (see combined.py) fuses them.
`ScoreResult` keeps the per-signal breakdown so notifications can explain *why*
an item surfaced — explicit diagnostics over opaque numbers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.domain.item import Item
from src.domain.profile import UserProfile


@dataclass
class ScoreResult:
    total: float                                  # fused score in [0, 1]
    breakdown: dict[str, float] = field(default_factory=dict)  # per-signal scores

    def explain(self) -> str:
        parts = ", ".join(f"{k}={v:.2f}" for k, v in self.breakdown.items())
        return f"total={self.total:.2f} ({parts})"


class Scorer(ABC):
    name: str = "scorer"

    @abstractmethod
    def score(self, item: Item, profile: UserProfile) -> float:
        """Return a relevance score in [0, 1]."""
