"""Semantic Scholar author-id resolution.

Maps free-text author display names to stable Semantic Scholar (S2) author ids
so downstream author matching can be id-based, not only name-based. arXiv has no
stable author ids, so we look each name up against the S2 author search API and
pick the best candidate.

Network access is isolated to :func:`resolve_author_ids`; the candidate-picking
logic lives in the pure :func:`_pick_best`, which has no I/O so it can be
unit-tested without hitting the network. ``requests`` is imported lazily inside
:func:`resolve_author_ids` to keep this module importable in environments where
HTTP isn't needed (and to avoid paying the import cost at module load).
"""

from __future__ import annotations

import logging
import time

from src.domain.item import normalize_name

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/author/search"

# Polite pause between per-name requests to stay friendly to the S2 API.
_SLEEP_BETWEEN = 0.5


def _pick_best(name: str, candidates: list[dict]) -> str | None:
    """Pick the best S2 author id for ``name`` from ``candidates``.

    ``candidates`` are S2 author objects of the shape
    ``{"authorId": str, "name": str, "paperCount": int}``.

    Selection policy (pure, no I/O):

    1. Prefer an exact normalized-name match (via
       :func:`src.domain.item.normalize_name`, which lowercases, strips accents
       and collapses whitespace). The first such candidate wins.
    2. Otherwise fall back to the candidate with the highest ``paperCount``
       (a missing/None count is treated as 0).
    3. An empty candidate list yields ``None``.

    Returns the chosen ``authorId`` as a ``str``, or ``None`` if nothing usable
    is found.
    """
    if not candidates:
        return None

    target = normalize_name(name)

    # 1. Exact normalized-name match wins, regardless of paperCount.
    for candidate in candidates:
        candidate_name = candidate.get("name")
        if candidate_name is None:
            continue
        if normalize_name(candidate_name) == target:
            author_id = candidate.get("authorId")
            return str(author_id) if author_id is not None else None

    # 2. Fall back to the most prolific candidate.
    def _paper_count(candidate: dict) -> int:
        count = candidate.get("paperCount")
        return count if isinstance(count, int) else 0

    best = max(candidates, key=_paper_count)
    author_id = best.get("authorId")
    return str(author_id) if author_id is not None else None


def resolve_author_ids(
    names: list[str],
    api_key: str | None = None,
    timeout: int = 20,
) -> dict[str, str | None]:
    """Resolve display names to Semantic Scholar author ids.

    For each name, queries the S2 author search endpoint and applies
    :func:`_pick_best` to the returned candidates. The ``x-api-key`` header is
    only sent when ``api_key`` is truthy. A short polite sleep separates
    successive requests.

    Failure isolation: any error while resolving a single name (network error,
    bad status, malformed payload) maps that name to ``None`` and never raises,
    so one bad name can't sink the whole batch.

    Returns a ``{name: authorId | None}`` mapping covering every input name.
    """
    # Lazy import so the module stays importable without `requests` available
    # and so import cost is only paid when we actually hit the network.
    import requests

    headers = {"x-api-key": api_key} if api_key else None

    results: dict[str, str | None] = {}
    for index, name in enumerate(names):
        if index > 0:
            time.sleep(_SLEEP_BETWEEN)
        try:
            resp = requests.get(
                _SEARCH_URL,
                params={"query": name, "fields": "name,paperCount"},
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            candidates = payload.get("data") or []
            results[name] = _pick_best(name, candidates)
        except Exception as exc:  # noqa: BLE001 - never let one name break the batch
            logger.warning("S2 author resolution failed for %r: %s", name, exc)
            results[name] = None

    return results
