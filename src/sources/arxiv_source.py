"""arXiv source adapter — the paper backbone.

Queries the public arXiv Atom API for the most recent submissions in a set of
categories, then keeps those published at/after `since`. arXiv has no stable
author ids (that's what Semantic Scholar is for in Phase 2), so author matching
here relies on display names.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import feedparser
import requests

from src.domain.item import Item
from src.sources.base import Source

logger = logging.getLogger(__name__)

_API_URL = "http://export.arxiv.org/api/query"

# Known venue acronyms we trust enough to surface. Order longest-first so a
# greedy alternation can't shadow a longer name (none currently overlap, but it
# keeps the intent explicit). Matched case-insensitively as a whole word.
_VENUE_ACRONYMS = (
    "NeurIPS", "ICML", "ICLR", "CVPR", "ECCV", "ICCV", "WACV",
    "ACL", "EMNLP", "NAACL", "COLING", "AAAI", "IJCAI", "AISTATS",
    "UAI", "COLT", "COLM", "TMLR", "JMLR", "KDD", "SIGGRAPH",
    "WWW", "SIGIR", "ICRA", "CoRL",
)

# Canonical casing keyed by the lowercased acronym, so we always emit the
# "pretty" form (e.g. "NeurIPS", not "neurips") regardless of how the comment
# spelled it.
_VENUE_CANONICAL = {a.lower(): a for a in _VENUE_ACRONYMS}

# A 4-digit year in the plausible-paper range (1900-2099).
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# One acronym as a whole word (word boundaries avoid matching e.g. "WWWeb"),
# case-insensitive. Built from the acronym list above.
_VENUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _VENUE_ACRONYMS) + r")\b",
    re.IGNORECASE,
)


def extract_venue(journal_ref: str | None, comment: str | None) -> str | None:
    """Best-effort publication venue from arXiv ``journal_ref`` / ``comment``.

    The returned venue is always the venue *name* only and never includes a
    year/date (e.g. "ICML", not "ICML 2026").

    Strategy (conservative — when unsure, return ``None``):

    1. If ``journal_ref`` is present and non-empty, trust it as the formal
       venue. It is whitespace-collapsed and any standalone 4-digit year token
       is stripped out (with leftover separators tidied up), since this field
       is author-curated and only set once a paper is actually published. If
       nothing meaningful remains after stripping the year, fall through to the
       comment heuristic.
    2. Otherwise scan ``comment`` (free text such as "Accepted at NeurIPS 2025"
       or "12 pages, 5 figures, accepted to ACL 2025") for a known venue
       acronym. If one is found, return just its canonical form (e.g. "ACL",
       "NeurIPS", "TMLR") — without any year.
    3. If neither yields a known venue, return ``None``.

    We deliberately do *not* try to invent a venue from phrases like
    "accepted to" alone: without a recognizable acronym the result would be
    guesswork, and omitting is better than showing garbage.
    """
    # 1. Formal journal reference wins when available.
    if journal_ref:
        cleaned = " ".join(journal_ref.split())
        if cleaned:
            stripped = _strip_year(cleaned)
            if stripped:
                return stripped

    # 2. Heuristic parse of the free-text comment.
    if not comment:
        return None
    match = _VENUE_RE.search(comment)
    if match is None:
        return None
    # Return the canonical acronym only; never append a year.
    return _VENUE_CANONICAL[match.group(1).lower()]


def _strip_year(text: str) -> str | None:
    """Remove standalone 4-digit year tokens from ``text`` and tidy up.

    Drops any ``19xx``/``20xx`` whole-word token, then collapses the double
    spaces and dangling separators (stray commas, periods, and empty
    parentheses) left where the year used to be. Returns ``None`` if nothing
    meaningful remains.
    """
    # Remove the year token(s).
    without_year = _YEAR_RE.sub("", text)
    # Collapse whitespace introduced by the removal.
    without_year = " ".join(without_year.split())
    # Drop now-empty parentheses left behind by e.g. "(2024)".
    without_year = re.sub(r"\(\s*\)", "", without_year)
    # Tidy spacing around the (possibly emptied) parentheses again.
    without_year = " ".join(without_year.split())
    # Remove separators that have nothing between them after losing the year,
    # e.g. "ICML ,  PMLR" -> "ICML, PMLR" and collapse ", ," -> ",".
    without_year = re.sub(r"\s+([,;.])", r"\1", without_year)
    without_year = re.sub(r"([,;.])(?:\s*[,;.])+", r"\1", without_year)
    # Strip leading/trailing separators and surrounding whitespace.
    without_year = without_year.strip(" ,;.")
    without_year = " ".join(without_year.split())
    return without_year or None


class ArxivSource(Source):
    name = "arxiv"

    def __init__(
        self,
        categories: list[str],
        max_results: int = 150,
        lookback_days: int = 2,
        timeout: int = 30,
    ) -> None:
        self.categories = categories
        self.max_results = max_results
        self.lookback_days = lookback_days
        self.timeout = timeout

    def fetch(self, since: datetime | None) -> list[Item]:
        query = " OR ".join(f"cat:{c}" for c in self.categories)
        params = {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": self.max_results,
        }
        try:
            resp = requests.get(_API_URL, params=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("arXiv fetch failed: %s", exc)
            return []
        # arXiv asks clients to wait ~3s between requests; we only do one here.
        time.sleep(1)

        feed = feedparser.parse(resp.text)
        items: list[Item] = []
        for entry in feed.entries:
            item = self._to_item(entry)
            if item is None:
                continue
            if since is not None and item.published < since:
                continue
            items.append(item)
        logger.info("arXiv: %d entries -> %d after date filter", len(feed.entries), len(items))
        return items

    @staticmethod
    def _to_item(entry) -> Item | None:
        try:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except (AttributeError, TypeError):
            return None
        # entry.id looks like http://arxiv.org/abs/2406.01234v1
        external_id = entry.id.rsplit("/abs/", 1)[-1]
        authors = tuple(a.get("name", "") for a in getattr(entry, "authors", []))
        categories = tuple(t.get("term", "") for t in getattr(entry, "tags", []))
        # arXiv-namespaced fields (may be absent); feedparser flattens them to
        # the `arxiv_`-prefixed keys.
        venue = extract_venue(entry.get("arxiv_journal_ref"), entry.get("arxiv_comment"))
        return Item(
            source="arxiv",
            external_id=external_id,
            title=" ".join(entry.title.split()),
            summary=" ".join(entry.summary.split()),
            url=entry.link,
            published=published,
            authors=authors,
            categories=categories,
            venue=venue,
        )
