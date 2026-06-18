"""SPECTER-based embedding backend.

Wraps a sentence-transformers SPECTER model. The model is loaded lazily on the
first real ``encode`` call so this module imports fine without torch /
sentence-transformers installed.
"""

from __future__ import annotations

import numpy as np

from src.embedding.base import Embedder


class SpecterEmbedder(Embedder):
    def __init__(
        self,
        model_name: str = "sentence-transformers/allenai-specter",
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        # Loaded lazily on first encode() to keep import cheap and torch-free.
        self._model = None

    def encode(self, texts: list[str]) -> "np.ndarray":
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        if self._model is None:
            # Lazy import: keep module importable without torch installed.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)

        return self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype(np.float32)
