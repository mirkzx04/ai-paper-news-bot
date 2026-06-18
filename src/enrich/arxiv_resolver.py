"""arXiv title-to-id resolution.

Maps a free-text paper *title* to its canonical arXiv id by querying the arXiv
API and picking the best-matching entry. Useful when a source (e.g. a Bluesky
post or a Semantic Scholar record) gives us a title but no arXiv id, so we can
still dedup against papers pulled directly from the arXiv source.

Network access is isolated to :func:`resolve_title_to_id`; the candidate-picking
logic lives in the pure :func:`_best_match`, which has no I/O so it can be
unit-tested without hitting the network. ``requests`` and ``feedparser`` are
imported lazily inside :func:`resolve_title_to_id` to keep this module
importable in environments where HTTP isn't needed (and to avoid paying the
import cost at module load).
"""

from __future__ import annotations

import logging

from src.domain.item import normalize_name

logger = logging.getLogger(__name__)

_API_URL = "http://export.arxiv.org/api/query"

# A candidate is accepted only if at least this fraction of the *query* tokens
# also appear in the candidate title. Tuned to avoid false positives: better to
# return nothing than to bind a title to the wrong paper.
_MATCH_THRESHOLD = 0.6


def _best_match(
    query_title: str,
    candidates: list[dict],
) -> tuple[str | None, str | None]:
    """Pick the best arXiv candidate for ``query_title`` (pure, no I/O).

    ``candidates`` are dicts of the shape ``{"arxiv_id": str, "title": str}``.

    Match rule:

    1. Normalize the query and every candidate title via
       :func:`src.domain.item.normalize_name` (lowercase, strip accents,
       collapse whitespace), then tokenize on whitespace into word *sets*.
    2. Score each candidate by word coverage of the query: the fraction of the
       query's distinct tokens that also appear in the candidate title.
    3. Accept the single top-scoring candidate only if its coverage is
       ``>= _MATCH_THRESHOLD``. Otherwise reject everything.

    Returns ``(arxiv_id, matched_title)`` of the accepted candidate, or
    ``(None, None)`` when there is no candidate, the query has no tokens, or no
    candidate clears the confidence threshold.
    """
    if not candidates:
        return None, None

    query_tokens = set(normalize_name(query_title).split())
    if not query_tokens:
        return None, None

    best_score = -1.0
    best_candidate: dict | None = None
    for candidate in candidates:
        candidate_title = candidate.get("title")
        if not candidate_title:
            continue
        candidate_tokens = set(normalize_name(candidate_title).split())
        if not candidate_tokens:
            continue
        # Coverage of the query by the candidate: how many of the words we are
        # looking for are present in this candidate's title.
        covered = len(query_tokens & candidate_tokens) / len(query_tokens)
        if covered > best_score:
            best_score = covered
            best_candidate = candidate

    if best_candidate is None or best_score < _MATCH_THRESHOLD:
        return None, None

    arxiv_id = best_candidate.get("arxiv_id")
    matched_title = best_candidate.get("title")
    if not arxiv_id or not matched_title:
        return None, None
    return str(arxiv_id), str(matched_title)


def resolve_title_to_id(
    title: str,
    timeout: int = 20,
) -> tuple[str | None, str | None]:
    """Resolve a paper title to its canonical arXiv id.

    Queries the arXiv API restricting the search to the title field and parses
    the Atom feed into candidates of the shape
    ``{"arxiv_id": <id without version>, "title": <clean title>}``, then applies
    :func:`_best_match` to pick a confident match.

    Failure isolation: any error (network error, bad status, malformed feed)
    logs a warning and yields ``(None, None)`` — this function never raises.

    Returns ``(arxiv_id, matched_title)`` for a confident match, else
    ``(None, None)``.
    """
    # Lazy imports so the module stays importable without these deps available
    # and so import cost is only paid when we actually hit the network.
    import feedparser
    import requests

    try:
        resp = requests.get(
            _API_URL,
            params={"search_query": f'ti:"{title}"', "max_results": 5},
            timeout=timeout,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        candidates: list[dict] = []
        for entry in feed.entries:
            entry_id = getattr(entry, "id", None)
            entry_title = getattr(entry, "title", None)
            if not entry_id or not entry_title:
                continue
            # entry.id is like "http://arxiv.org/abs/1706.03762v5"; strip the
            # "/abs/" prefix and the trailing version suffix to get the
            # canonical id.
            arxiv_id = entry_id.rsplit("/abs/", 1)[-1].split("v")[0]
            clean_title = " ".join(entry_title.split())
            candidates.append({"arxiv_id": arxiv_id, "title": clean_title})

        return _best_match(title, candidates)
    except Exception as exc:  # noqa: BLE001 - never raise on resolution failure
        logger.warning("arXiv title resolution failed for %r: %s", title, exc)
        return None, None
