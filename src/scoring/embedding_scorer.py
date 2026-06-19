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

Feedback loop (👍/👎)
---------------------
On top of the onboarding seeds (declared interests, fixed implicit weight 1.0)
the scorer optionally accepts *dynamic* feedback vectors built from the user's
votes (see `src/embedding/feedback_vectors.py`):

  - `pos_vectors`/`pos_weights`: 👍 papers become extra positive seeds with a
    weight `< 1` (so the declared profile stays dominant — anchoring/stability).
  - `neg_vectors`/`neg_weights`: 👎 papers become a *soft, margined* penalty.

Both channels are confined to this embedding scorer and are explicitly
**balanced**: positives can only raise the score, negatives only lower it, and
the asymmetry below keeps the negative conservative.

For an item with embedding ``e`` write the per-vector contribution of a vector
``v`` with weight ``w`` against baseline ``β`` as
``w · clamp01((cos(e, v) − β) / (1 − β))``. Then:

  - ``pos_term = max`` over {onboarding seeds (w=1, β=b)} ∪ {pos (w=wⱼ, β=b)},
    or 0 when there is neither a seed nor a positive vector;
  - ``neg_term = max_k ( w_k · relu((cos(e, neg_k) − b_neg) / (1 − b_neg)) )``,
    or 0 when there is no negative vector;
  - ``s_emb = clamp01(pos_term − λ · neg_term)``.

Two parameters encode the intended asymmetry:

  - ``b_neg > b`` (0.80 > 0.75) is a **margin**: the negative term only fires for
    papers that are *very* close to a 👎, not merely above the positive baseline.
  - ``λ < 1`` (0.5) makes a 👎 less forceful than a 👍, because a single negative
    vote is a semantically noisier signal than a positive one.

Backward compatibility: with the feedback inputs left at their ``None`` defaults
this reduces *exactly* to the original behaviour — ``pos_term`` is the max over
the seeds alone, ``neg_term`` is 0, so ``s_emb`` equals the old
``clamp01((best_cos − baseline) / (1 − baseline))`` (and ``None`` seeds still
short-circuit to 0.0 before the embedder is touched).

The embedder is duck-typed (`.encode(list[str]) -> np.ndarray`) and never
imported here; when there is nothing positive to compare against
(`seed_vectors is None` and no `pos_vectors`) AND there are no `neg_vectors`, we
short-circuit *before* touching it, so the heavy model never loads.
"""

from __future__ import annotations

import numpy as np

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.scoring.base import Scorer


class EmbeddingScorer(Scorer):
    name = "embedding"

    def __init__(
        self,
        embedder,
        seed_vectors: np.ndarray | None,
        baseline: float = 0.75,
        *,
        pos_vectors: np.ndarray | None = None,
        pos_weights: np.ndarray | None = None,
        neg_vectors: np.ndarray | None = None,
        neg_weights: np.ndarray | None = None,
        baseline_neg: float = 0.80,
        neg_lambda: float = 0.5,
    ) -> None:
        # embedder is duck-typed: .encode(list[str]) -> np.ndarray.
        # seed_vectors is a (n_seeds, dim) L2-normalized matrix, or None if no seeds.
        self.embedder = embedder
        self.seed_vectors = seed_vectors
        self.baseline = baseline

        # Dynamic feedback channels (👍/👎). Each is a (k, dim) L2-normalized
        # matrix with a matching (k,) weight array, or None when that channel is
        # empty. Normalize empties to None so `score` has a single sentinel.
        self.pos_vectors, self.pos_weights = _clean_channel(pos_vectors, pos_weights)
        self.neg_vectors, self.neg_weights = _clean_channel(neg_vectors, neg_weights)

        # b_neg > baseline is the margin; neg_lambda < 1 keeps 👎 conservative.
        self.baseline_neg = baseline_neg
        self.neg_lambda = neg_lambda

    def score(self, item: Item, profile: UserProfile) -> float:
        has_positive = self.seed_vectors is not None or self.pos_vectors is not None
        has_negative = self.neg_vectors is not None
        # Nothing to compare against on either side -> no semantic signal. Return
        # WITHOUT calling the embedder so the model is never loaded for nothing.
        if not has_positive and not has_negative:
            return 0.0

        emb = self.embedder.encode([item.text()])[0]

        # pos_term: best baseline-rescaled, weighted contribution over the union
        # of onboarding seeds (w=1, β=baseline) and positive feedback vectors
        # (w=wⱼ, β=baseline). The max (not a sum) means a burst of votes on one
        # topic cannot inflate the score past the single best contribution —
        # this is the structural stand-in for an explicit saturation term.
        pos_term = 0.0
        if self.seed_vectors is not None:
            seed_cos = self.seed_vectors @ emb
            pos_term = _rescale_clamped(float(np.max(seed_cos)), self.baseline)
        if self.pos_vectors is not None:
            pos_cos = self.pos_vectors @ emb
            pos_contrib = self.pos_weights * _rescale_clamped_vec(pos_cos, self.baseline)
            pos_term = max(pos_term, float(np.max(pos_contrib)))

        # neg_term: weighted relu contribution against the *higher* baseline_neg
        # margin, max over the negative vectors. relu (not clamp01) is enough
        # here — the upper clamp happens on the final s_emb.
        neg_term = 0.0
        if self.neg_vectors is not None:
            neg_cos = self.neg_vectors @ emb
            neg_contrib = self.neg_weights * _relu_rescale_vec(neg_cos, self.baseline_neg)
            neg_term = float(np.max(neg_contrib))

        s_emb = pos_term - self.neg_lambda * neg_term
        return max(0.0, min(1.0, s_emb))


# --- helpers ----------------------------------------------------------------

def _clean_channel(vectors, weights):
    """Normalize a (vectors, weights) feedback channel to a usable pair or Nones.

    Returns ``(None, None)`` when the channel is empty (``vectors is None`` or
    has zero rows) so `score` can test a single sentinel. Otherwise returns the
    vectors as a float32 array and the weights as a float32 array, validating
    that the row counts match.
    """
    if vectors is None:
        return None, None
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.shape[0] == 0:
        return None, None
    if weights is None:
        weights = np.ones(vectors.shape[0], dtype=np.float32)
    else:
        weights = np.asarray(weights, dtype=np.float32).reshape(-1)
    if weights.shape[0] != vectors.shape[0]:
        raise ValueError(
            f"feedback weights ({weights.shape[0]}) and vectors "
            f"({vectors.shape[0]}) length mismatch"
        )
    return vectors, weights


def _rescale_clamped(cosine: float, baseline: float) -> float:
    """``clamp01((cosine - baseline) / (1 - baseline))`` for a scalar."""
    rescaled = (cosine - baseline) / (1.0 - baseline)
    return max(0.0, min(1.0, rescaled))


def _rescale_clamped_vec(cosines: "np.ndarray", baseline: float) -> "np.ndarray":
    """Vectorized ``clamp01((cos - baseline) / (1 - baseline))``."""
    rescaled = (cosines - baseline) / (1.0 - baseline)
    return np.clip(rescaled, 0.0, 1.0)


def _relu_rescale_vec(cosines: "np.ndarray", baseline: float) -> "np.ndarray":
    """Vectorized ``relu((cos - baseline) / (1 - baseline))`` (no upper clamp)."""
    rescaled = (cosines - baseline) / (1.0 - baseline)
    return np.maximum(0.0, rescaled)
