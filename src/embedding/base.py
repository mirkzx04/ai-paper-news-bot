"""Embedding abstractions.

An `Embedder` maps a batch of texts to a dense matrix of row-wise
L2-normalized float32 vectors, so downstream cosine similarity reduces to a
dot product. The concrete backends (see specter.py) load heavy models lazily;
this module stays import-safe without torch installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Embedder(ABC):
    @abstractmethod
    def encode(self, texts: list[str]) -> "np.ndarray":
        """Encode ``texts`` into a matrix of shape ``(len(texts), dim)``.

        The returned array is ``float32`` and each row is L2-normalized.
        """


def l2_normalize(matrix: "np.ndarray") -> "np.ndarray":
    """Row-wise L2 normalization.

    A zero row stays zero (no NaN, no divide-by-zero): the per-row norm is
    clamped away from zero only where it is non-positive, leaving the all-zero
    numerator untouched.
    """
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # Replace zero (or non-finite) norms with 1.0 so the corresponding rows —
    # which are themselves all-zero — divide cleanly to all-zero instead of NaN.
    safe_norms = np.where(norms > 0.0, norms, 1.0)
    return (matrix / safe_norms).astype(np.float32)
