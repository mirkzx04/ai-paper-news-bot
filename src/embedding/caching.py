"""CachingEmbedder — memoize embeddings so a candidate is encoded once per run.

In a per-user digest fan-out the SAME candidate papers are scored for every user
(only each user's profile/feedback vectors differ). Without caching, the dominant
cost — running SPECTER on each candidate's text — is paid once *per user*. Wrapping
the shared embedder in this cache makes each distinct text encode exactly once and
reused across all users in the run, an ~N× reduction in embedding work for N users.

It is a transparent decorator: same ``encode(list[str]) -> np.ndarray`` contract
(float32, L2-normalized rows, order preserved), so it drops in anywhere an
``Embedder`` is expected. The cache is keyed by the exact text; identical texts
return identical rows (SPECTER is deterministic). Memory is bounded by the number
of distinct texts seen in a run — fine for a single digest, and a fresh instance
per run keeps it from growing unbounded across runs.
"""

from __future__ import annotations

import numpy as np

from src.embedding.base import Embedder


class CachingEmbedder(Embedder):
    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._cache: dict[str, np.ndarray] = {}

    def encode(self, texts: list[str]) -> "np.ndarray":
        if not texts:
            return self._embedder.encode(texts)

        # Identify which texts we haven't embedded yet, preserving first-seen order
        # and de-duplicating within this call.
        missing: list[str] = []
        seen: set[str] = set()
        for t in texts:
            if t not in self._cache and t not in seen:
                missing.append(t)
                seen.add(t)

        if missing:
            vectors = self._embedder.encode(missing)
            for t, vec in zip(missing, vectors):
                self._cache[t] = np.asarray(vec, dtype=np.float32)

        return np.stack([self._cache[t] for t in texts]).astype(np.float32)
