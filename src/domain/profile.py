"""Domain entity `UserProfile` — what a user wants to hear about.

Designed multi-user from the start (`user_id` keyed), even though the v1
deployment only ever builds one profile (Mirko's). The `embedding` profile
vector is populated in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """Absolute contribution of each signal: total = min(1, Σ wᵢ·sᵢ).

    Tuned so a single strong signal can surface an item on its own:
    a perfect keyword match (~0.5) clears the digest threshold; a followed
    author (1.0) always alerts; embedding kicks in from Phase 2.
    """

    keyword: float = 0.6     # 1 title keyword (~0.63) -> ~0.38 -> clears digest
    author: float = 1.0      # a followed author alone is enough to alert
    embedding: float = 0.6   # used from Phase 2 onward


@dataclass
class UserProfile:
    user_id: str
    keywords: tuple[str, ...] = ()
    author_ids: tuple[str, ...] = ()      # S2 stable ids (resolved in Phase 2)
    author_names: tuple[str, ...] = ()    # display-name fallback (arXiv has no ids)
    seed_arxiv_ids: tuple[str, ...] = ()  # used to build the profile vector (Phase 2)
    conferences: tuple[str, ...] = ()     # favorite venues (stored now; scored later)
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    # embedding: np.ndarray | None = None   # added in Phase 2
