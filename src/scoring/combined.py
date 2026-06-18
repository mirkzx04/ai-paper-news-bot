"""CombinedScorer — fuse per-signal scores into one relevance score.

Fusion is a *saturating weighted sum*:  total = min(1, Σ wᵢ·sᵢ).
The weights are absolute contributions (not a convex combination), so a single
strong signal can surface an item on its own — e.g. a clear keyword match scores
well even when the embedding signal is inactive (Phase 1) and there's no author
hit. This avoids the failure mode of a normalized average, where high-weight but
absent signals would dilute everything to near zero.

The *routing* decision (alert vs digest) lives in the pipeline, not here — in
particular a followed-author match always alerts, independent of `total`.
Keeping the ranking score separate from the routing rule keeps both transparent.
"""

from __future__ import annotations

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.scoring.base import ScoreResult, Scorer


class CombinedScorer:
    def __init__(self, scorers: dict[str, Scorer]) -> None:
        self.scorers = scorers

    def score(self, item: Item, profile: UserProfile) -> ScoreResult:
        breakdown = {name: s.score(item, profile) for name, s in self.scorers.items()}
        weights = profile.weights
        total = sum(breakdown[name] * getattr(weights, name, 0.0) for name in breakdown)
        return ScoreResult(total=min(1.0, total), breakdown=breakdown)
