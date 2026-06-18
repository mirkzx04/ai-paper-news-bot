"""Author scorer — binary signal: is this (co)authored by someone you follow?

Prefers stable Semantic Scholar author ids (resolved in Phase 2); falls back to
normalized display-name matching, which is all arXiv gives us.
"""

from __future__ import annotations

from src.domain.item import Item, normalize_name
from src.domain.profile import UserProfile
from src.scoring.base import Scorer


class AuthorScorer(Scorer):
    name = "author"

    def score(self, item: Item, profile: UserProfile) -> float:
        if profile.author_ids and set(item.author_ids) & set(profile.author_ids):
            return 1.0
        if profile.author_names:
            wanted = {normalize_name(n) for n in profile.author_names}
            if any(normalize_name(a) in wanted for a in item.authors):
                return 1.0
        return 0.0
