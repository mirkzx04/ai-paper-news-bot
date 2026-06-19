"""Tests for the EmbeddingScorer feedback-loop formula and backward-compat.

Covers the scorer math (seeds-only / pos-only / neg-only / pos+neg / the b_neg
margin / λ asymmetry / no-embedder short-circuit) and — critically — a numeric
proof that with the feedback inputs at their None defaults the scorer is *bit-
for-bit identical* to the original pre-feedback formula.

Pure stdlib `unittest` (no pytest); run with `python -m unittest`.
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.scoring.embedding_scorer import EmbeddingScorer


def _unit(vec) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32)
    return (vec / np.linalg.norm(vec)).astype(np.float32)


class _FakeEmbedder:
    """Maps a known text to a fixed (already L2-normalized) vector.

    `encode([text])` returns the row registered for that text. Lets us drive the
    cosine to any value we want without loading SPECTER.
    """

    def __init__(self, mapping: dict[str, np.ndarray]) -> None:
        self.mapping = {k: _unit(v) for k, v in mapping.items()}
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        return np.stack([self.mapping[t] for t in texts]).astype(np.float32)


def _item(text: str) -> Item:
    # Item.text() == "title\n\nsummary"; we stash everything in title so the
    # FakeEmbedder key is exactly what we register.
    return Item(source="arxiv", external_id="x", title=text, summary="",
                url="", published=datetime(2024, 1, 1))


def _profile() -> UserProfile:
    return UserProfile(user_id="t")


def _original_formula(cos: float, baseline: float) -> float:
    """The pre-feedback reference: clamp01((cos - baseline)/(1 - baseline))."""
    rescaled = (cos - baseline) / (1.0 - baseline)
    return max(0.0, min(1.0, rescaled))


class BackwardCompatTest(unittest.TestCase):
    """With feedback inputs as None defaults, score == the original formula."""

    def _item_text(self) -> str:
        return "title\n\n"  # Item.text() strips trailing whitespace -> "title"

    def test_identical_to_original_across_cosines(self) -> None:
        baseline = 0.75
        seed = _unit([1.0, 0.0, 0.0, 0.0])
        # Sweep target cosines spanning below/at/above baseline and clamp edges.
        for target_cos in [0.0, 0.5, 0.74, 0.75, 0.76, 0.80, 0.9, 0.999, 1.0]:
            # Build an item embedding with exactly `target_cos` to the seed:
            # e = c*seed + sqrt(1-c^2)*orth, both unit and orthogonal.
            orth = _unit([0.0, 1.0, 0.0, 0.0])
            c = target_cos
            e = _unit(c * seed + math.sqrt(max(0.0, 1.0 - c * c)) * orth)
            text = f"item_{target_cos}"
            emb = _FakeEmbedder({text: e})
            scorer = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline)
            got = scorer.score(_item(text), _profile())
            expected = _original_formula(float(np.dot(seed, e)), baseline)
            self.assertAlmostEqual(got, expected, places=6,
                                   msg=f"target_cos={target_cos}")

    def test_none_seed_short_circuits_without_embedder(self) -> None:
        # seed_vectors None and no pos/neg -> 0.0, embedder NEVER touched.
        emb = _FakeEmbedder({})  # empty mapping; any encode() would KeyError
        scorer = EmbeddingScorer(emb, None)
        self.assertEqual(scorer.score(_item("anything"), _profile()), 0.0)
        self.assertEqual(emb.calls, 0)

    def test_empty_feedback_channels_are_none_equivalent(self) -> None:
        # Passing zero-row matrices must behave exactly like None (no effect).
        baseline = 0.75
        seed = _unit([1.0, 0.0])
        e = _unit([0.9, 0.2])
        text = "x"
        emb = _FakeEmbedder({text: e})
        base = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline)
        empty = EmbeddingScorer(
            emb, np.stack([seed]), baseline=baseline,
            pos_vectors=np.zeros((0, 2), np.float32),
            neg_vectors=np.zeros((0, 2), np.float32),
        )
        self.assertAlmostEqual(base.score(_item(text), _profile()),
                               empty.score(_item(text), _profile()), places=6)


class PositiveTermTest(unittest.TestCase):
    def test_pos_only_no_seeds(self) -> None:
        # No onboarding seeds; a single 👍 vector drives the score.
        baseline = 0.75
        pos = _unit([1.0, 0.0])
        e = _unit([0.85, math.sqrt(1 - 0.85**2)])  # cos to pos = 0.85
        text = "p"
        emb = _FakeEmbedder({text: e})
        # weight 0.6 -> contribution = 0.6 * (0.85-0.75)/0.25 = 0.6*0.4 = 0.24
        scorer = EmbeddingScorer(emb, None, baseline=baseline,
                                 pos_vectors=np.stack([pos]),
                                 pos_weights=np.array([0.6], np.float32))
        self.assertAlmostEqual(scorer.score(_item(text), _profile()), 0.24, places=6)

    def test_pos_term_is_max_over_seed_and_pos(self) -> None:
        # Seed gives a higher contribution than the (downweighted) pos -> seed wins.
        baseline = 0.75
        seed = _unit([1.0, 0.0])         # cos to e below; full weight 1.0
        pos = _unit([0.0, 1.0])          # cos to e high but weight 0.6
        # e closer to pos than seed, but pos is downweighted.
        e = _unit([0.5, 0.9])
        cos_seed = float(np.dot(seed, e))
        cos_pos = float(np.dot(pos, e))
        seed_contrib = _original_formula(cos_seed, baseline)               # w=1
        pos_contrib = 0.6 * _original_formula(cos_pos, baseline)           # w=0.6
        expected = max(seed_contrib, pos_contrib)
        text = "m"
        emb = _FakeEmbedder({text: e})
        scorer = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline,
                                 pos_vectors=np.stack([pos]),
                                 pos_weights=np.array([0.6], np.float32))
        self.assertAlmostEqual(scorer.score(_item(text), _profile()), expected, places=6)

    def test_burst_of_pos_does_not_exceed_single_best_max_semantics(self) -> None:
        # Five identical 👍 vectors must score the SAME as one (max, not sum):
        # the structural "saturation".
        baseline = 0.75
        pos = _unit([1.0, 0.0])
        e = _unit([0.9, math.sqrt(1 - 0.9**2)])
        text = "b"
        emb = _FakeEmbedder({text: e})
        one = EmbeddingScorer(emb, None, baseline=baseline,
                              pos_vectors=np.stack([pos]),
                              pos_weights=np.array([0.6], np.float32))
        five = EmbeddingScorer(emb, None, baseline=baseline,
                               pos_vectors=np.stack([pos] * 5),
                               pos_weights=np.array([0.6] * 5, np.float32))
        self.assertAlmostEqual(one.score(_item(text), _profile()),
                               five.score(_item(text), _profile()), places=6)


class NegativeTermTest(unittest.TestCase):
    def test_neg_only_pos_term_zero(self) -> None:
        # No seeds, no pos -> pos_term 0; a close 👎 pushes s_emb to 0 (clamped).
        b_neg, lam = 0.80, 0.5
        neg = _unit([1.0, 0.0])
        e = _unit([0.95, math.sqrt(1 - 0.95**2)])  # cos to neg = 0.95
        text = "n"
        emb = _FakeEmbedder({text: e})
        scorer = EmbeddingScorer(emb, None, neg_vectors=np.stack([neg]),
                                 neg_weights=np.array([0.6], np.float32),
                                 baseline_neg=b_neg, neg_lambda=lam)
        # pos_term=0, neg_term=0.6*(0.95-0.80)/0.20=0.6*0.75=0.45 -> 0-0.5*0.45<0 -> 0
        self.assertEqual(scorer.score(_item(text), _profile()), 0.0)

    def test_margin_b_neg_blocks_mild_negatives(self) -> None:
        # cos to neg = 0.78: above the positive baseline 0.75 but BELOW b_neg 0.80
        # -> neg_term must be 0, so the pos signal is untouched.
        baseline, b_neg, lam = 0.75, 0.80, 0.5
        seed = _unit([1.0, 0.0])
        neg = _unit([1.0, 0.0])  # same direction as seed for this test
        e = _unit([0.78, math.sqrt(1 - 0.78**2)])
        text = "mg"
        emb = _FakeEmbedder({text: e})
        with_neg = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline,
                                   neg_vectors=np.stack([neg]),
                                   neg_weights=np.array([0.6], np.float32),
                                   baseline_neg=b_neg, neg_lambda=lam)
        without = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline)
        # cos 0.78 < b_neg 0.80 -> neg_term 0 -> identical to no-neg scorer.
        self.assertAlmostEqual(with_neg.score(_item(text), _profile()),
                               without.score(_item(text), _profile()), places=6)
        # And it is the positive contribution (0.78-0.75)/0.25 = 0.12.
        self.assertAlmostEqual(with_neg.score(_item(text), _profile()), 0.12, places=6)

    def test_pos_and_neg_combined_with_lambda_asymmetry(self) -> None:
        # A non-trivial pos+neg case: the item is strongly above the positive
        # baseline AND past the negative margin (realistic: a 👎 paper can sit
        # near a seed), so s_emb = pos_term - λ·neg_term is a real subtraction.
        # We pick the embedding directly and read its actual cosines, then check
        # the scorer reproduces the formula exactly.
        baseline, b_neg, lam = 0.75, 0.80, 0.5
        seed = _unit([1.0, 0.10, 0.0, 0.0])
        neg = _unit([0.95, 0.0, 0.30, 0.0])  # partly aligned with seed
        e = _unit([0.97, 0.05, 0.22, 0.05])
        cos_seed = float(np.dot(seed, e))
        cos_neg = float(np.dot(neg, e))
        # Confirm the regime this test is meant to exercise.
        self.assertGreater(cos_seed, baseline)   # positive contribution > 0
        self.assertGreater(cos_neg, b_neg)       # negative clears the margin
        text = "pn"
        emb = _FakeEmbedder({text: e})
        scorer = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline,
                                 neg_vectors=np.stack([neg]),
                                 neg_weights=np.array([0.6], np.float32),
                                 baseline_neg=b_neg, neg_lambda=lam)
        pos_term = _original_formula(cos_seed, baseline)
        neg_term = 0.6 * max(0.0, (cos_neg - b_neg) / (1 - b_neg))
        expected = max(0.0, min(1.0, pos_term - lam * neg_term))
        self.assertAlmostEqual(scorer.score(_item(text), _profile()), expected, places=6)
        # The subtraction is non-trivial: the penalty strictly lowers the score
        # below pos_term, and λ=0.5 makes it milder than the raw neg_term.
        self.assertGreater(neg_term, 0.0)
        self.assertLess(expected, pos_term)
        self.assertLess(lam * neg_term, neg_term)

    def test_neg_term_is_max_over_negatives(self) -> None:
        # Two negatives; the closer one (larger contribution) must dominate.
        baseline, b_neg, lam = 0.75, 0.80, 0.5
        seed = _unit([1.0, 0.0, 0.0])
        neg_far = _unit([0.0, 1.0, 0.0])
        neg_close = _unit([0.0, 0.0, 1.0])
        e = _unit([0.5, 0.82, 0.97])  # closer to neg_close
        text = "mx"
        emb = _FakeEmbedder({text: e})
        scorer = EmbeddingScorer(emb, np.stack([seed]), baseline=baseline,
                                 neg_vectors=np.stack([neg_far, neg_close]),
                                 neg_weights=np.array([0.6, 0.6], np.float32),
                                 baseline_neg=b_neg, neg_lambda=lam)
        cos_seed = float(np.dot(seed, e))
        c_far = float(np.dot(neg_far, e))
        c_close = float(np.dot(neg_close, e))
        neg_term = max(0.6 * max(0.0, (c_far - b_neg) / (1 - b_neg)),
                       0.6 * max(0.0, (c_close - b_neg) / (1 - b_neg)))
        expected = max(0.0, min(1.0, _original_formula(cos_seed, baseline) - lam * neg_term))
        self.assertAlmostEqual(scorer.score(_item(text), _profile()), expected, places=6)


class ConstructorTest(unittest.TestCase):
    def test_weight_length_mismatch_raises(self) -> None:
        v = np.zeros((2, 3), np.float32)
        with self.assertRaises(ValueError):
            EmbeddingScorer(object(), None, pos_vectors=v,
                            pos_weights=np.array([1.0], np.float32))

    def test_default_weights_are_ones(self) -> None:
        v = np.zeros((3, 4), np.float32)
        s = EmbeddingScorer(object(), None, pos_vectors=v)
        self.assertEqual(s.pos_weights.tolist(), [1.0, 1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
