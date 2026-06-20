"""Tests for the optional observer hook added to `ProfileStore`.

Two guarantees:
  1. Default ``listener=None`` => behaviour identical to before (no notifications,
     mutators still return newly-added/removed items, state still persists).
  2. With a listener injected => exactly one ``(action, kind, value)`` callback
     per *real* change, across every mutator, including the topic edge cases and
     a faulty listener (which must never break a mutation).

Stdlib `unittest`; run with `python -m unittest`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store.preference_dataset import PreferenceDataset, ProfileListener
from src.store.profile_store import ProfileStore, UserProfileStoreProvider


class _Recorder:
    """A listener that records every (action, kind, value) it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, action: str, kind: str, value: str) -> None:
        self.calls.append((action, kind, value))


class DefaultBehaviourUnchangedTest(unittest.TestCase):
    """listener=None must reproduce the exact prior behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "overlay.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mutators_return_values_and_persist(self) -> None:
        store = ProfileStore(self.path)  # no listener
        self.assertEqual(store.add_authors(["A", "B", "A"]), ["A", "B"])
        self.assertEqual(store.add_keywords(["moe"]), ["moe"])
        self.assertEqual(store.add_conferences(["NeurIPS"]), ["NeurIPS"])
        self.assertEqual(store.add_seed_ids(["2101.03961"]), ["2101.03961"])
        self.assertEqual(store.add_topic("Interp", ["sae", "probing"]), (True, ["sae", "probing"]))
        self.assertEqual(store.remove_authors(["A"]), ["A"])

        # State reloads from disk (persistence unaffected by the new code path).
        reloaded = ProfileStore(self.path)
        self.assertEqual(reloaded.authors, ["B"])
        self.assertEqual(reloaded.keywords, ["moe"])
        self.assertEqual(reloaded.seeds, ["2101.03961"])
        self.assertEqual(reloaded.topics, {"Interp": ["sae", "probing"]})

    def test_no_op_returns_emit_nothing_observable(self) -> None:
        store = ProfileStore(self.path)
        store.add_authors(["A"])
        # Re-adding the same author returns [] (dedup), as before.
        self.assertEqual(store.add_authors(["a"]), [])
        # Removing something absent returns [].
        self.assertEqual(store.remove_keywords(["nope"]), [])


class ObserverNotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "overlay.json")
        self.rec = _Recorder()
        self.store = ProfileStore(self.path, listener=self.rec)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_list_kinds_notify_once_per_new_item(self) -> None:
        self.store.add_authors(["Neel Nanda", "Chris Olah"])
        self.store.add_keywords(["moe"])
        self.store.add_conferences(["ICLR"])
        self.store.add_seed_ids(["2101.03961"])
        self.assertEqual(self.rec.calls, [
            ("add", "author", "Neel Nanda"),
            ("add", "author", "Chris Olah"),
            ("add", "keyword", "moe"),
            ("add", "conference", "ICLR"),
            ("add", "seed", "2101.03961"),
        ])

    def test_duplicate_add_does_not_notify(self) -> None:
        self.store.add_authors(["A"])
        self.rec.calls.clear()
        self.store.add_authors(["a", "A"])  # both dups (case-insensitive)
        self.assertEqual(self.rec.calls, [])

    def test_remove_notifies_only_for_real_removals(self) -> None:
        self.store.add_authors(["A", "B"])
        self.rec.calls.clear()
        self.store.remove_authors(["A", "ghost"])  # only A exists
        self.assertEqual(self.rec.calls, [("remove", "author", "A")])

    def test_add_topic_notifies_per_keyword(self) -> None:
        created, added = self.store.add_topic("Interp", ["sae", "probing"])
        self.assertEqual((created, added), (True, ["sae", "probing"]))
        self.assertEqual(self.rec.calls, [
            ("add", "topic", "sae"),
            ("add", "topic", "probing"),
        ])

    def test_add_topic_without_keywords_emits_topic_name(self) -> None:
        # A topic created with no keywords still produces one signal (the name),
        # so the creation isn't silently lost.
        self.store.add_topic("EmptyTopic", [])
        self.assertEqual(self.rec.calls, [("add", "topic", "EmptyTopic")])

    def test_extending_existing_topic_only_notifies_new_keywords(self) -> None:
        self.store.add_topic("T", ["a"])
        self.rec.calls.clear()
        self.store.add_topic("t", ["a", "b"])  # 'a' already there, only 'b' is new
        self.assertEqual(self.rec.calls, [("add", "topic", "b")])

    def test_remove_topic_keywords_notifies_per_removed(self) -> None:
        self.store.add_topic("T", ["a", "b", "c"])
        self.rec.calls.clear()
        outcome, removed = self.store.remove_topic("T", ["a", "c", "z"])
        self.assertEqual(outcome, "keywords_removed")
        self.assertEqual(removed, ["a", "c"])
        self.assertEqual(self.rec.calls, [
            ("remove", "topic", "a"),
            ("remove", "topic", "c"),
        ])

    def test_remove_whole_topic_notifies_each_keyword(self) -> None:
        self.store.add_topic("T", ["a", "b"])
        self.rec.calls.clear()
        outcome, _ = self.store.remove_topic("T", [])  # no kws => drop whole topic
        self.assertEqual(outcome, "topic_removed")
        self.assertEqual(self.rec.calls, [
            ("remove", "topic", "a"),
            ("remove", "topic", "b"),
        ])

    def test_remove_empty_topic_emits_topic_name(self) -> None:
        self.store.add_topic("Empty", [])
        self.rec.calls.clear()
        self.store.remove_topic("Empty", [])
        self.assertEqual(self.rec.calls, [("remove", "topic", "Empty")])

    def test_remove_absent_topic_notifies_nothing(self) -> None:
        outcome, _ = self.store.remove_topic("Nope", [])
        self.assertEqual(outcome, "not_found")
        self.assertEqual(self.rec.calls, [])

    def test_faulty_listener_never_breaks_mutation(self) -> None:
        def boom(action, kind, value):
            raise RuntimeError("listener exploded")

        store = ProfileStore(os.path.join(self.tmp.name, "ov2.json"), listener=boom)
        # The mutation must still succeed and persist despite the raising listener.
        self.assertEqual(store.add_authors(["Safe"]), ["Safe"])
        self.assertEqual(ProfileStore(os.path.join(self.tmp.name, "ov2.json")).authors,
                         ["Safe"])


class ProfileStoreToDatasetIntegrationTest(unittest.TestCase):
    """End-to-end: ProfileStore + ProfileListener actually write JSONL events."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = PreferenceDataset(os.path.join(self.tmp.name, "preferences.jsonl"))
        self.store = ProfileStore(
            os.path.join(self.tmp.name, "overlay.json"),
            listener=ProfileListener(self.ds),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_profile_edits_land_in_dataset(self) -> None:
        self.store.add_authors(["Neel Nanda"])
        self.store.add_keywords(["moe", "routing"])
        self.store.remove_keywords(["moe"])

        events = self.ds.events()
        self.assertEqual(
            [(e["type"], e["kind"], e["value"]) for e in events],
            [
                ("profile_add", "author", "Neel Nanda"),
                ("profile_add", "keyword", "moe"),
                ("profile_add", "keyword", "routing"),
                ("profile_remove", "keyword", "moe"),
            ],
        )
        # Every event carries a ts.
        self.assertTrue(all("ts" in e for e in events))

    def test_profile_listener_can_attach_anonymous_user_id(self) -> None:
        store = ProfileStore(
            os.path.join(self.tmp.name, "user_overlay.json"),
            listener=ProfileListener(self.ds, user_id="u_anon"),
        )
        store.add_keywords(["privacy"])

        event = self.ds.events()[0]
        self.assertEqual(event["user_id"], "u_anon")
        self.assertNotIn("username", event)

    def test_user_profile_provider_isolates_paths(self) -> None:
        provider = UserProfileStoreProvider(
            os.path.join(self.tmp.name, "profile_overlay.json"),
            listener_factory=lambda user_id: ProfileListener(self.ds, user_id=user_id),
        )
        provider.for_user("u_a").add_keywords(["alpha"])
        provider.for_user("u_b").add_keywords(["beta"])

        self.assertEqual(ProfileStore(provider.path_for("u_a")).keywords, ["alpha"])
        self.assertEqual(ProfileStore(provider.path_for("u_b")).keywords, ["beta"])
        self.assertEqual([e["user_id"] for e in self.ds.events()], ["u_a", "u_b"])


if __name__ == "__main__":
    unittest.main()
