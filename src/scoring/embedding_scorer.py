"""Embedding-similarity scorer — semantic relevance via cosine similarity.

Embeds the item's text and scores it by its similarity to the user's seed
papers. Two deliberate design choices, both motivated by how SPECTER embeddings
behave (verified empirically):

1. **Max over seeds, not centroid.** `seed_vectors` is a (n_seeds, dim) matrix
   (one L2-normalized row per seed). We score by the cosine to the *closest*
   seed, so a paper that strongly matches one of the user's heterogeneous
   interests ranks high instead of being averaged down by the others.

2. **Baseline subtraction.** SPECTER's cosine space is anisotropic: unrelated ML
   papers still sit at ~0.80-0.85 cosine. Raw cosine would therefore score
   almost everything ~0.85 and flood the digest. We rescale
   `(cos - baseline) / (1 - baseline)` clamped to [0, 1], so only similarity
   *above* the baseline counts. `baseline` is tunable (default 0.75, chosen via
   tools/eval_ranking.py — 0.80 was too aggressive and zeroed borderline hits).

The embedder is duck-typed (`.encode(list[str]) -> np.ndarray`) and never
imported here; when there are no seeds (`seed_vectors is None`) we short-circuit
*before* touching it, so the heavy model never loads.
"""

from __future__ import annotations

import numpy as np

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.scoring.base import Scorer


class EmbeddingScorer(Scorer):
    name = "embedding"

    def __init__(self, embedder, seed_vectors: np.ndarray | None,
                 baseline: float = 0.75) -> None:
        # embedder is duck-typed: .encode(list[str]) -> np.ndarray.
        # seed_vectors is a (n_seeds, dim) L2-normalized matrix, or None if no seeds.
        self.embedder = embedder
        self.seed_vectors = seed_vectors
        self.baseline = baseline

    def score(self, item: Item, profile: UserProfile) -> float:
        # No seeds -> no semantic signal. Return WITHOUT calling the embedder so
        # the model is never loaded when there is nothing to compare against.
        if self.seed_vectors is None:
            return 0.0

        emb = self.embedder.encode([item.text()])[0]
        # Cosine to each seed (rows are L2-normalized, so dot == cosine); take
        # the closest seed.
        best_cosine = float(np.max(self.seed_vectors @ emb))
        # Anisotropy rescale: subtract the baseline and renormalize to [0, 1].
        rescaled = (best_cosine - self.baseline) / (1.0 - self.baseline)
        return max(0.0, min(1.0, rescaled))
