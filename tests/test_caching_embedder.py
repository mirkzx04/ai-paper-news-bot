"""Tests for CachingEmbedder — each distinct text is encoded exactly once."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.embedding.caching import CachingEmbedder  # noqa: E402


class _CountingEmbedder:
    """Returns a deterministic vector per text and counts the texts it encodes."""

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, texts):
        self.encoded.extend(texts)
        return np.array([[float(len(t)), 1.0] for t in texts], dtype=np.float32)


class CachingEmbedderTest(unittest.TestCase):
    def test_repeated_text_encoded_once_across_calls(self) -> None:
        base = _CountingEmbedder()
        cache = CachingEmbedder(base)
        v1 = cache.encode(["paper A"])
        v2 = cache.encode(["paper A"])           # second user, same candidate
        np.testing.assert_array_equal(v1, v2)
        self.assertEqual(base.encoded, ["paper A"])   # underlying called once only

    def test_mixed_batch_only_encodes_misses(self) -> None:
        base = _CountingEmbedder()
        cache = CachingEmbedder(base)
        cache.encode(["A", "B"])
        base.encoded.clear()
        out = cache.encode(["A", "C", "B"])      # only C is new
        self.assertEqual(base.encoded, ["C"])
        self.assertEqual(out.shape, (3, 2))

    def test_order_preserved(self) -> None:
        cache = CachingEmbedder(_CountingEmbedder())
        out = cache.encode(["xx", "y", "zzz"])
        # vector[0] encodes len -> first column is the text length.
        self.assertEqual(list(out[:, 0]), [2.0, 1.0, 3.0])


if __name__ == "__main__":
    unittest.main()
