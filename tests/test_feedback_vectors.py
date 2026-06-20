"""Tests for `build_feedback_vectors` / `load_or_build_feedback_vectors`.

Covers: net-state (last-vote-wins, including up->down flips), text=None drop
that still counts toward cold-start N, per-vote weight = w_pos_max·decay·coldstart
(decay and cold-start checked numerically), the per-class cap M (ring buffer
keeps the most recent/strongest), pos/neg splitting, and the embedding cache
(re-embed only new/changed papers; deterministic `now` injection).

Pure stdlib `unittest`; run with `python -m unittest`.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.embedding.feedback_vectors import (
    build_feedback_vectors,
    load_or_build_feedback_vectors,
)
from src.store.preference_dataset import PreferenceDataset

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


class _CountingEmbedder:
    """Deterministic embedder that records how many texts it embedded.

    Each distinct text maps to a fixed pseudo-random unit row (hash-seeded), so
    cache hits are observable via `embedded_texts`.
    """

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.embedded_texts: list[str] = []

    def encode(self, texts):
        self.embedded_texts.extend(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.standard_normal(self.dim).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


def _vote(signal: str, key: str, text: str | None, ts: datetime) -> dict:
    return {"type": "vote", "signal": signal, "canonical_key": key,
            "text": text, "ts": ts.isoformat()}


class NetStateTest(unittest.TestCase):
    def test_last_vote_wins_up_then_down(self) -> None:
        events = [
            _vote("up", "arxiv:1", "paper one", NOW - timedelta(days=1)),
            _vote("down", "arxiv:1", "paper one", NOW),  # later -> wins
        ]
        emb = _CountingEmbedder()
        pos_v, pos_w, neg_v, neg_w = build_feedback_vectors(events, emb, now=NOW)
        # The paper ends up as a negative, not a positive.
        self.assertIsNone(pos_v)
        self.assertIsNone(pos_w)
        self.assertEqual(neg_v.shape[0], 1)

    def test_down_then_up_flips_to_positive(self) -> None:
        events = [
            _vote("down", "arxiv:2", "p2", NOW - timedelta(days=2)),
            _vote("up", "arxiv:2", "p2", NOW - timedelta(days=1)),
        ]
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = build_feedback_vectors(events, emb, now=NOW)
        self.assertEqual(pos_v.shape[0], 1)
        self.assertIsNone(neg_v)

    def test_pos_neg_split(self) -> None:
        events = [
            _vote("up", "arxiv:1", "a", NOW),
            _vote("up", "arxiv:2", "b", NOW),
            _vote("down", "arxiv:3", "c", NOW),
        ]
        emb = _CountingEmbedder()
        pos_v, pos_w, neg_v, neg_w = build_feedback_vectors(events, emb, now=NOW)
        self.assertEqual(pos_v.shape[0], 2)
        self.assertEqual(pos_w.shape[0], 2)
        self.assertEqual(neg_v.shape[0], 1)
        self.assertEqual(neg_w.shape[0], 1)

    def test_text_none_dropped_but_counts_for_coldstart(self) -> None:
        # 4 distinct votes: one has text=None (dropped from embedding) but all 4
        # count toward N for cold-start. With K=5, coldstart = 4/5 = 0.8.
        events = [
            _vote("up", "arxiv:1", "a", NOW),
            _vote("up", "arxiv:2", "b", NOW),
            _vote("up", "arxiv:3", None, NOW),     # not embeddable
            _vote("down", "arxiv:4", "d", NOW),
        ]
        emb = _CountingEmbedder()
        pos_v, pos_w, neg_v, neg_w = build_feedback_vectors(
            events, emb, now=NOW, w_pos_max=0.6, coldstart_k=5)
        # Only 3 papers embedded (text=None dropped).
        self.assertEqual(len(emb.embedded_texts), 3)
        self.assertEqual(pos_v.shape[0], 2)
        # Weight at age 0: w_pos_max * 1.0 * (4/5) = 0.6 * 0.8 = 0.48.
        self.assertTrue(np.allclose(pos_w, 0.48, atol=1e-6))
        self.assertTrue(np.allclose(neg_w, 0.48, atol=1e-6))

    def test_empty_or_malformed_yields_none(self) -> None:
        emb = _CountingEmbedder()
        self.assertEqual(build_feedback_vectors([], emb, now=NOW),
                         (None, None, None, None))
        bad = [{"type": "vote", "signal": "sideways", "canonical_key": "k", "text": "x"}]
        self.assertEqual(build_feedback_vectors(bad, emb, now=NOW),
                         (None, None, None, None))
        # And the embedder is never called for an all-malformed set.
        self.assertEqual(emb.embedded_texts, [])

    def test_toggle_off_excludes_key_from_both_classes_and_coldstart(self) -> None:
        # Refinement 2: a final "none" (withdrawn vote) removes the key entirely
        # — not a positive, not a negative, and NOT counted toward cold-start N.
        events = [
            _vote("up", "arxiv:1", "a", NOW - timedelta(days=1)),
            _vote("none", "arxiv:1", "a", NOW),       # withdraw -> key gone
            _vote("up", "arxiv:2", "b", NOW),         # the only surviving vote
        ]
        emb = _CountingEmbedder()
        pos_v, pos_w, neg_v, neg_w = build_feedback_vectors(
            events, emb, now=NOW, w_pos_max=0.6, coldstart_k=5)
        # Only arxiv:2 survives as a positive; arxiv:1 is fully excluded.
        self.assertEqual(pos_v.shape[0], 1)
        self.assertIsNone(neg_v)
        # Cold-start N counts ONLY the active vote (N=1 -> coldstart 1/5=0.2),
        # i.e. the withdrawn key does NOT inflate N (else it would be 2/5=0.4).
        self.assertTrue(np.allclose(pos_w, 0.6 * (1 / 5), atol=1e-6))
        # The withdrawn paper is never embedded.
        self.assertEqual(emb.embedded_texts, ["b"])

    def test_toggle_off_then_revote_counts_again(self) -> None:
        # up -> none -> down: the LAST event wins, so the key is a negative again.
        events = [
            _vote("up", "arxiv:1", "a", NOW - timedelta(days=2)),
            _vote("none", "arxiv:1", "a", NOW - timedelta(days=1)),
            _vote("down", "arxiv:1", "a", NOW),
        ]
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = build_feedback_vectors(events, emb, now=NOW)
        self.assertIsNone(pos_v)
        self.assertEqual(neg_v.shape[0], 1)

    def test_all_votes_withdrawn_yields_none(self) -> None:
        events = [
            _vote("up", "arxiv:1", "a", NOW - timedelta(days=1)),
            _vote("none", "arxiv:1", "a", NOW),
        ]
        emb = _CountingEmbedder()
        self.assertEqual(build_feedback_vectors(events, emb, now=NOW),
                         (None, None, None, None))
        self.assertEqual(emb.embedded_texts, [])  # nothing embeddable


class WeightTest(unittest.TestCase):
    def test_decay_is_exponential_in_age(self) -> None:
        tau = 120.0
        age_days = 60.0
        events = [_vote("up", "arxiv:1", "a", NOW - timedelta(days=age_days))]
        emb = _CountingEmbedder()
        # coldstart_k=1 so coldstart=1 and we isolate the decay factor.
        _, pos_w, _, _ = build_feedback_vectors(
            events, emb, now=NOW, w_pos_max=0.6, tau_days=tau, coldstart_k=1)
        expected = 0.6 * math.exp(-age_days / tau)
        self.assertAlmostEqual(float(pos_w[0]), expected, places=6)

    def test_coldstart_grows_to_one(self) -> None:
        emb = _CountingEmbedder()
        # 2 votes, K=5 -> coldstart 0.4; weight = 0.6*1*0.4 = 0.24.
        events2 = [_vote("up", f"arxiv:{i}", f"t{i}", NOW) for i in range(2)]
        _, w2, _, _ = build_feedback_vectors(events2, emb, now=NOW,
                                             w_pos_max=0.6, coldstart_k=5)
        self.assertTrue(np.allclose(w2, 0.24, atol=1e-6))
        # 5 votes, K=5 -> coldstart capped at 1.0; weight = 0.6.
        emb2 = _CountingEmbedder()
        events5 = [_vote("up", f"arxiv:{i}", f"t{i}", NOW) for i in range(5)]
        _, w5, _, _ = build_feedback_vectors(events5, emb2, now=NOW,
                                             w_pos_max=0.6, coldstart_k=5)
        self.assertTrue(np.allclose(w5, 0.6, atol=1e-6))
        # 10 votes, K=5 -> still capped at 1.0 (not >1).
        emb3 = _CountingEmbedder()
        events10 = [_vote("up", f"arxiv:{i}", f"t{i}", NOW) for i in range(10)]
        _, w10, _, _ = build_feedback_vectors(events10, emb3, now=NOW,
                                              w_pos_max=0.6, coldstart_k=5)
        self.assertTrue(np.allclose(w10, 0.6, atol=1e-6))


class CapTest(unittest.TestCase):
    def test_cap_m_keeps_most_recent_per_class(self) -> None:
        # 4 positives at increasing age; cap_m=2 must keep the two NEWEST
        # (highest weight, since weight is monotone decreasing in age).
        cap = 2
        votes = []
        for i in range(4):
            # i=0 oldest (age 30d), i=3 newest (age 0d).
            age = (3 - i) * 10
            votes.append(_vote("up", f"arxiv:{i}", f"text{i}", NOW - timedelta(days=age)))
        emb = _CountingEmbedder()
        pos_v, pos_w, _, _ = build_feedback_vectors(
            events=votes, embedder=emb, now=NOW, w_pos_max=0.6,
            tau_days=120.0, coldstart_k=1, cap_m=cap)
        self.assertEqual(pos_v.shape[0], cap)
        self.assertEqual(pos_w.shape[0], cap)
        # The two kept weights must be the two LARGEST of the four.
        all_ages = [(3 - i) * 10 for i in range(4)]
        all_w = sorted(0.6 * math.exp(-a / 120.0) for a in all_ages)
        kept_expected = sorted(all_w[-cap:])
        self.assertTrue(np.allclose(sorted(pos_w.tolist()), kept_expected, atol=1e-6))

    def test_cap_applies_per_class_independently(self) -> None:
        # 3 pos + 3 neg, cap_m=2 -> each class capped to 2 independently.
        votes = []
        for i in range(3):
            votes.append(_vote("up", f"p{i}", f"pt{i}", NOW - timedelta(days=i)))
            votes.append(_vote("down", f"n{i}", f"nt{i}", NOW - timedelta(days=i)))
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = build_feedback_vectors(
            votes, emb, now=NOW, coldstart_k=1, cap_m=2)
        self.assertEqual(pos_v.shape[0], 2)
        self.assertEqual(neg_v.shape[0], 2)


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "nested", "feedback_vectors.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reembeds_only_new_or_changed(self) -> None:
        events = [
            _vote("up", "arxiv:1", "first paper", NOW),
            _vote("up", "arxiv:2", "second paper", NOW),
        ]
        emb1 = _CountingEmbedder()
        build_feedback_vectors(events, emb1, now=NOW, cache_path=self.cache)
        self.assertEqual(sorted(emb1.embedded_texts), ["first paper", "second paper"])

        # Second run: add a new paper, keep the two existing unchanged.
        events2 = events + [_vote("up", "arxiv:3", "third paper", NOW)]
        emb2 = _CountingEmbedder()
        build_feedback_vectors(events2, emb2, now=NOW, cache_path=self.cache)
        # Only the NEW paper re-embedded; the two cached ones reused.
        self.assertEqual(emb2.embedded_texts, ["third paper"])

    def test_changed_text_is_reembedded(self) -> None:
        events = [_vote("up", "arxiv:1", "original", NOW)]
        emb1 = _CountingEmbedder()
        build_feedback_vectors(events, emb1, now=NOW, cache_path=self.cache)
        # Same key, different text -> must re-embed.
        events2 = [_vote("up", "arxiv:1", "edited abstract", NOW)]
        emb2 = _CountingEmbedder()
        build_feedback_vectors(events2, emb2, now=NOW, cache_path=self.cache)
        self.assertEqual(emb2.embedded_texts, ["edited abstract"])

    def test_vectors_are_l2_normalized(self) -> None:
        events = [_vote("up", "arxiv:1", "p", NOW), _vote("down", "arxiv:2", "q", NOW)]
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = build_feedback_vectors(events, emb, now=NOW)
        self.assertAlmostEqual(float(np.linalg.norm(pos_v[0])), 1.0, places=5)
        self.assertAlmostEqual(float(np.linalg.norm(neg_v[0])), 1.0, places=5)


class LoadOrBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ds_path = os.path.join(self.tmp.name, "preferences.jsonl")
        self.cache = os.path.join(self.tmp.name, "feedback_vectors.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_votes_from_dataset(self) -> None:
        ds = PreferenceDataset(self.ds_path)
        # log() stamps ts itself; we don't control it, but `now` defaulting is
        # fine here since we only assert shapes, not exact weights.
        ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:1", "text": "a"})
        ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:2", "text": "b"})
        # Non-vote events must be ignored.
        ds.log({"type": "profile_add", "kind": "author", "value": "X"})
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = load_or_build_feedback_vectors(
            ds, emb, cache_path=self.cache, now=NOW)
        self.assertEqual(pos_v.shape[0], 1)
        self.assertEqual(neg_v.shape[0], 1)

    def test_accepts_path_string(self) -> None:
        ds = PreferenceDataset(self.ds_path)
        ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:1", "text": "a"})
        emb = _CountingEmbedder()
        pos_v, _, _, _ = load_or_build_feedback_vectors(
            self.ds_path, emb, cache_path=self.cache, now=NOW)
        self.assertEqual(pos_v.shape[0], 1)

    def test_filters_votes_by_anonymous_user_id(self) -> None:
        ds = PreferenceDataset(self.ds_path)
        ds.log({"type": "vote", "user_id": "u_1", "signal": "up",
                "canonical_key": "arxiv:1", "text": "a"})
        ds.log({"type": "vote", "user_id": "u_2", "signal": "down",
                "canonical_key": "arxiv:2", "text": "b"})
        emb = _CountingEmbedder()
        pos_v, _, neg_v, _ = load_or_build_feedback_vectors(
            ds, emb, cache_path=self.cache, now=NOW, user_id="u_1")
        self.assertEqual(pos_v.shape[0], 1)
        self.assertIsNone(neg_v)
        self.assertEqual(emb.embedded_texts, ["a"])

    def test_no_votes_returns_all_none(self) -> None:
        ds = PreferenceDataset(self.ds_path)
        emb = _CountingEmbedder()
        self.assertEqual(
            load_or_build_feedback_vectors(ds, emb, cache_path=self.cache, now=NOW),
            (None, None, None, None),
        )

    def test_impressions_never_feed_scoring(self) -> None:
        # CRITICAL CONTRACT (Refinement 3): `impression` events are eval-only
        # weak negatives and MUST NOT influence the embedding scorer. Adding
        # impressions to the dataset must leave the (pos/neg) feedback vectors
        # byte-for-byte identical to the votes-only result — otherwise a shown-
        # but-unvoted paper would penalise similar papers and collapse diversity.
        ds = PreferenceDataset(self.ds_path)
        ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:1", "text": "a"})
        ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:2", "text": "b"})
        emb_votes = _CountingEmbedder()
        base = load_or_build_feedback_vectors(ds, emb_votes, cache_path=self.cache, now=NOW)

        # Now flood the SAME dataset with impressions (some on the voted papers,
        # some on brand-new papers) and rebuild with a fresh cache.
        for i in range(5):
            ds.log({"type": "impression", "canonical_key": f"arxiv:imp{i}",
                    "score": 0.9, "breakdown": {"embedding": 0.9}, "route": "digest"})
        ds.log({"type": "impression", "canonical_key": "arxiv:1",
                "score": 0.4, "breakdown": {"embedding": 0.4}, "route": "alert"})
        cache2 = os.path.join(self.tmp.name, "fv2.json")
        emb_after = _CountingEmbedder()
        after = load_or_build_feedback_vectors(ds, emb_after, cache_path=cache2, now=NOW)

        # Same number of texts embedded (only the 2 voted papers, never impressions).
        self.assertEqual(sorted(emb_after.embedded_texts), ["a", "b"])
        # And the vectors/weights are unchanged across the two builds.
        pos_v0, pos_w0, neg_v0, neg_w0 = base
        pos_v1, pos_w1, neg_v1, neg_w1 = after
        for a, b in ((pos_v0, pos_v1), (pos_w0, pos_w1), (neg_v0, neg_v1), (neg_w0, neg_w1)):
            self.assertTrue(np.array_equal(a, b))


if __name__ == "__main__":
    unittest.main()
