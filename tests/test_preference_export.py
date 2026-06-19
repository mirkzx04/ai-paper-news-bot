"""Tests for `tools/preference_export.export_labels`.

Verifies the labelled-set export: seed ids become strong-ish positives, author/
keyword/topic adds become weak positive terms, a later removal withdraws the
positive (net state), conferences are excluded, and the (currently empty) vote
label path activates the moment `vote` events appear.

Stdlib `unittest`; run with `python -m unittest`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))  # preference_export lives in tools/

from preference_export import export_labels
from src.store.preference_dataset import PreferenceDataset


class ExportLabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = PreferenceDataset(os.path.join(self.tmp.name, "preferences.jsonl"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_dataset_exports_empty_structures(self) -> None:
        labels = export_labels(self.ds)
        self.assertEqual(labels["seed_positives"], [])
        self.assertEqual(labels["weak_pos_terms"], {"author": [], "keyword": [], "topic": []})
        self.assertEqual(labels["vote_positives"], [])
        self.assertEqual(labels["vote_negatives"], [])

    def test_seed_adds_become_positives(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "2101.03961"})
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "2201.02177"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["seed_positives"], ["2101.03961", "2201.02177"])

    def test_weak_terms_bucketed_by_kind(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "author", "value": "Neel Nanda"})
        self.ds.log({"type": "profile_add", "kind": "keyword", "value": "moe"})
        self.ds.log({"type": "profile_add", "kind": "topic", "value": "sae"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["weak_pos_terms"], {
            "author": ["Neel Nanda"],
            "keyword": ["moe"],
            "topic": ["sae"],
        })

    def test_removal_withdraws_positive_net_state(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "keyword", "value": "moe"})
        self.ds.log({"type": "profile_add", "kind": "keyword", "value": "routing"})
        self.ds.log({"type": "profile_remove", "kind": "keyword", "value": "moe"})
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "2101.03961"})
        self.ds.log({"type": "profile_remove", "kind": "seed", "value": "2101.03961"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["weak_pos_terms"]["keyword"], ["routing"])
        self.assertEqual(labels["seed_positives"], [])  # added then removed -> gone

    def test_conferences_are_not_exported_as_labels(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "conference", "value": "NeurIPS"})
        labels = export_labels(self.ds)
        self.assertNotIn("conference", labels["weak_pos_terms"])
        self.assertEqual(labels["seed_positives"], [])

    def test_duplicate_adds_dedup_preserving_order(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "B"})
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "A"})
        self.ds.log({"type": "profile_add", "kind": "seed", "value": "B"})
        self.assertEqual(export_labels(self.ds)["seed_positives"], ["B", "A"])

    def test_vote_events_populate_strong_labels(self) -> None:
        # Forward-compat: once the feedback loop writes votes, they appear here
        # with NO change to the exporter.
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:2101.03961"})
        self.ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:1406.2661"})
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:2201.02177"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["vote_positives"], ["arxiv:2101.03961", "arxiv:2201.02177"])
        self.assertEqual(labels["vote_negatives"], ["arxiv:1406.2661"])

    def test_malformed_events_are_ignored(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "seed"})           # no value
        self.ds.log({"type": "profile_add", "kind": "seed", "value": ""})  # empty value
        self.ds.log({"type": "vote", "signal": "up"})                  # no canonical_key
        labels = export_labels(self.ds)
        self.assertEqual(labels["seed_positives"], [])
        self.assertEqual(labels["vote_positives"], [])

    def test_toggle_off_vote_drops_from_both_vote_lists(self) -> None:
        # Refinement 2: a vote later withdrawn (last signal "none") is no longer a
        # label — excluded from BOTH positives and negatives (net-state coherence).
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:A"})
        self.ds.log({"type": "vote", "signal": "none", "canonical_key": "arxiv:A"})  # withdrawn
        self.ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:B"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["vote_positives"], [])      # A withdrawn
        self.assertEqual(labels["vote_negatives"], ["arxiv:B"])

    def test_vote_flip_resolves_to_final_signal_only(self) -> None:
        # up -> down lands the key ONLY in negatives (net-state), not in both.
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:C"})
        self.ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:C"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["vote_positives"], [])
        self.assertEqual(labels["vote_negatives"], ["arxiv:C"])

    def test_impressions_become_weak_negatives_excluding_voted(self) -> None:
        # Refinement 3: shown-but-unvoted papers are WEAK negatives, distinct from
        # explicit 👎. A paper that was both shown AND voted is excluded (it's a
        # judged paper, not "ignored").
        self.ds.log({"type": "impression", "canonical_key": "arxiv:shown1", "route": "digest"})
        self.ds.log({"type": "impression", "canonical_key": "arxiv:shown2", "route": "alert"})
        self.ds.log({"type": "impression", "canonical_key": "arxiv:voted", "route": "digest"})
        self.ds.log({"type": "vote", "signal": "down", "canonical_key": "arxiv:voted"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["weak_negatives"], ["arxiv:shown1", "arxiv:shown2"])
        self.assertEqual(labels["vote_negatives"], ["arxiv:voted"])
        # weak_negatives is strictly disjoint from the explicit vote negatives.
        self.assertNotIn("arxiv:voted", labels["weak_negatives"])

    def test_withdrawn_vote_paper_is_not_weak_negative(self) -> None:
        # A paper shown, voted, then withdrawn ("none") was still JUDGED, so it is
        # excluded from weak_negatives (any vote event marks the key as judged).
        self.ds.log({"type": "impression", "canonical_key": "arxiv:X", "route": "digest"})
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:X"})
        self.ds.log({"type": "vote", "signal": "none", "canonical_key": "arxiv:X"})
        labels = export_labels(self.ds)
        self.assertEqual(labels["weak_negatives"], [])
        self.assertEqual(labels["vote_positives"], [])  # withdrawn, so not a +

    def test_empty_dataset_has_empty_weak_negatives(self) -> None:
        self.assertEqual(export_labels(self.ds)["weak_negatives"], [])


if __name__ == "__main__":
    unittest.main()
