"""Keyword scorer — cheap, high-recall prefilter."""

from __future__ import annotations

import math
import re

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.scoring.base import Scorer


class KeywordScorer(Scorer):
    """Saturating function of weighted keyword hits.

    Title hits count double (a keyword in the title is a stronger signal than
    one buried in the abstract). Score = 1 - exp(-hits/scale), so it grows fast
    on the first hit and saturates toward 1:

        1 title keyword  (hits=2) -> ~0.63
        2 title keywords (hits=4) -> ~0.86
        1 body keyword   (hits=1) -> ~0.39
    """

    name = "keyword"

    def __init__(self, scale: float = 2.0) -> None:
        self.scale = scale

    def score(self, item: Item, profile: UserProfile) -> float:
        if not profile.keywords:
            return 0.0
        title = item.title.lower()
        body = item.summary.lower()
        hits = 0.0
        for keyword in profile.keywords:
            needle = keyword.lower()
            if _contains(title, needle):
                hits += 2.0
            elif _contains(body, needle):
                hits += 1.0
        if hits == 0.0:
            return 0.0
        return 1.0 - math.exp(-hits / self.scale)


def _contains(haystack: str, needle: str) -> bool:
    """Word-boundary match (so 'moe' doesn't fire inside 'smoementum')."""
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
