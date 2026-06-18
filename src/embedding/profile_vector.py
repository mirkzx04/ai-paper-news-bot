"""Build and cache the user's profile embeddings from seed arXiv papers.

The "profile" is a MATRIX with one L2-normalized embedding row per hand-picked
"seed" arXiv paper. We keep the seeds separate (rather than averaging them into
one centroid) so the scorer can match a candidate against its CLOSEST single
seed: a researcher's interests are heterogeneous (MoE, interpretability, ...),
and a centroid smears them together, penalizing a paper that strongly matches
one interest but is far from the others.

Building it requires (a) fetching the seed papers' title+abstract text from the
arXiv API and (b) embedding that text with a heavy model. Both are expensive and
the result only changes when the seed set changes, so :func:`load_or_build`
caches the vector on disk keyed by the seed-id set.

``requests``/``feedparser`` are imported lazily inside :func:`fetch_arxiv_summaries`
so this module stays import-safe in environments where only the cached vector is
loaded (or where those packages are absent).
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np

from src.embedding.base import l2_normalize

logger = logging.getLogger(__name__)

_API_URL = "http://export.arxiv.org/api/query"


def fetch_arxiv_summaries(arxiv_ids: list[str], timeout: int = 30) -> list[str]:
    """Fetch ``"title\\n\\nabstract"`` text for each seed arXiv id.

    Queries the arXiv Atom API with the given ids and returns one
    whitespace-collapsed ``"title\\n\\nabstract"`` string per returned entry, in
    the order arXiv reports them. Returns ``[]`` on empty input or on any request
    failure (a warning is logged; this never raises), so callers can treat an
    empty result as "no data" without special-casing network errors.
    """
    if not arxiv_ids:
        return []

    # Lazy imports: keep the module importable without these packages and
    # without paying their import cost when only the cache is loaded.
    import requests
    import feedparser

    params = {
        "id_list": ",".join(arxiv_ids),
        "max_results": len(arxiv_ids),
    }
    try:
        resp = requests.get(_API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("arXiv summary fetch failed: %s", exc)
        return []

    feed = feedparser.parse(resp.text)
    summaries: list[str] = []
    for entry in feed.entries:
        title = " ".join(entry.get("title", "").split())
        abstract = " ".join(entry.get("summary", "").split())
        summaries.append(f"{title}\n\n{abstract}")
    return summaries


def build_profile_vector(seed_arxiv_ids: list[str], embedder) -> "np.ndarray | None":
    """Embed the seed papers into a matrix of L2-normalized row vectors.

    Returns ``None`` when there are no seed ids or when the arXiv fetch yields no
    summaries. Otherwise returns a ``(n_seeds, dim)`` ``float32`` array with one
    L2-normalized row per seed (kept separate, not averaged — see module docstring).
    """
    if not seed_arxiv_ids:
        return None

    summaries = fetch_arxiv_summaries(seed_arxiv_ids)
    if not summaries:
        return None

    embeddings = np.asarray(embedder.encode(summaries), dtype=np.float32)
    # Ensure rows are unit-norm (the embedder normally normalizes already).
    return l2_normalize(embeddings).astype(np.float32)


def load_or_build(
    seed_arxiv_ids: list[str], embedder, path: str
) -> "np.ndarray | None":
    """Return the cached profile vector, rebuilding it only when the seeds change.

    The cache at ``path`` is JSON of the form
    ``{"seed_ids": [...], "vectors": [[...], ...]}``. If it exists and its
    ``seed_ids`` *set* matches the requested set, the stored matrix is returned
    directly (no embedding, no network). Otherwise it is rebuilt via
    :func:`build_profile_vector`; on success the cache is (re)written — creating
    its parent directory if needed — and the matrix returned. If the rebuild
    yields ``None`` the cache is left untouched. An old/incompatible cache (e.g.
    a previous single-vector format) is treated as unreadable and rebuilt.
    """
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            cached_ids = cached["seed_ids"]
            vectors = cached["vectors"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Profile-vector cache at %s unreadable, rebuilding: %s", path, exc)
        else:
            if set(cached_ids) == set(seed_arxiv_ids):
                return np.array(vectors, dtype=np.float32)

    vectors = build_profile_vector(seed_arxiv_ids, embedder)
    if vectors is None:
        return None

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"seed_ids": list(seed_arxiv_ids), "vectors": vectors.tolist()}, fh)
    return vectors
