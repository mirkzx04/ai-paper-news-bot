"""Research-field classifier — labels an item by keyword-topic membership.

Unlike the keyword scorer (which produces a single relevance score), this maps
an item onto one or more *research fields* defined by topic -> keyword lists.
Matching reuses the keyword scorer's word-boundary helper so behavior stays
consistent across the codebase.
"""

from __future__ import annotations

from src.domain.item import Item
from src.scoring.keyword_scorer import _contains

# Coarse-area fallback: arXiv primary category -> human-readable research area.
# Only consulted when NO topic keyword matches (see `classify`).
_CATEGORY_AREAS: dict[str, str] = {
    "cs.CV": "Computer Vision",
    "cs.CL": "NLP",
    "cs.LG": "Machine Learning",
    "stat.ML": "Machine Learning",
    "cs.AI": "AI",
}


class FieldClassifier:
    """Assigns research-field labels to an item from a topic taxonomy.

    A field matches if any of its keywords appears (word-boundary,
    case-insensitive) in the item's title or summary. Fields are ranked by a hit
    score where title hits count double (mirroring `KeywordScorer`), strongest
    first. Fields with zero hits are dropped.
    """

    def __init__(self, topics: dict[str, list[str]]) -> None:
        self.topics = topics

    def classify(self, item: Item) -> list[str]:
        """Return research-field labels, strongest first (possibly empty)."""
        title = item.title.lower()
        body = item.summary.lower()

        scored: list[tuple[str, float]] = []
        for field_name, keywords in self.topics.items():
            score = 0.0
            for keyword in keywords:
                needle = keyword.lower()
                if _contains(title, needle):
                    score += 2.0
                elif _contains(body, needle):
                    score += 1.0
            if score > 0.0:
                scored.append((field_name, score))

        if scored:
            # Sort by score descending; preserve insertion order on ties (stable sort).
            scored.sort(key=lambda pair: pair[1], reverse=True)
            return [field_name for field_name, _ in scored]

        # Fallback: no topic matched -> map the primary arXiv category to a
        # coarse area. Topic matches always take precedence over this branch.
        if item.categories:
            area = _CATEGORY_AREAS.get(item.categories[0])
            if area is not None:
                return [area]
        return []
