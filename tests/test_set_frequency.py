"""Tests for the `/set_frequency` command and the `digest_frequency` preference.

Covers, with stdlib `unittest`:
  * ProfileStore: default value, getter, set/validation, persistence across
    reloads, and backward-compat (an overlay JSON written WITHOUT the field, and
    one with a garbage value, both load and yield the default without losing the
    other fields).
  * SetFrequencyCommand: empty args shows current + options; synonyms (English +
    Italian, short forms, case-insensitive) map to the right canonical value and
    persist; unrecognized input replies with the options and never raises.

Run with `python -m unittest`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.commands.set_frequency import SetFrequencyCommand
from src.store.profile_store import ProfileStore


class DigestFrequencyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "overlay.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_2x_daily(self) -> None:
        store = ProfileStore(self.path)
        self.assertEqual(store.digest_frequency, "2x_daily")

    def test_set_valid_persists_and_returns_true(self) -> None:
        store = ProfileStore(self.path)
        for value in ("daily", "weekly", "2x_daily"):
            self.assertTrue(store.set_digest_frequency(value))
            self.assertEqual(store.digest_frequency, value)
        # Value survives a reload from disk.
        store.set_digest_frequency("weekly")
        self.assertEqual(ProfileStore(self.path).digest_frequency, "weekly")

    def test_set_invalid_returns_false_and_keeps_previous(self) -> None:
        store = ProfileStore(self.path)
        store.set_digest_frequency("daily")
        for bad in ("hourly", "2x", "", "DAILY", "monthly", "2"):
            self.assertFalse(store.set_digest_frequency(bad))
            # Unchanged: invalid input must not mutate the stored value.
            self.assertEqual(store.digest_frequency, "daily")

    def test_backward_compat_overlay_without_field(self) -> None:
        # Simulate a pre-existing overlay that predates digest_frequency.
        legacy = {"authors": ["Neel Nanda"], "keywords": ["moe"],
                  "topics": {"Interp": ["sae"]}, "conferences": ["ICLR"],
                  "seeds": ["2101.03961"]}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)

        store = ProfileStore(self.path)
        # Default applied, and no other field is lost.
        self.assertEqual(store.digest_frequency, "2x_daily")
        self.assertEqual(store.authors, ["Neel Nanda"])
        self.assertEqual(store.keywords, ["moe"])
        self.assertEqual(store.topics, {"Interp": ["sae"]})
        self.assertEqual(store.conferences, ["ICLR"])
        self.assertEqual(store.seeds, ["2101.03961"])

    def test_corrupted_value_falls_back_to_default(self) -> None:
        # A non-canonical persisted value must not poison the store.
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"authors": ["A"], "digest_frequency": "fortnightly"}, fh)
        store = ProfileStore(self.path)
        self.assertEqual(store.digest_frequency, "2x_daily")
        self.assertEqual(store.authors, ["A"])  # other fields intact

    def test_setting_does_not_clobber_other_fields(self) -> None:
        store = ProfileStore(self.path)
        store.add_authors(["A"])
        store.add_keywords(["moe"])
        store.set_digest_frequency("weekly")
        reloaded = ProfileStore(self.path)
        self.assertEqual(reloaded.authors, ["A"])
        self.assertEqual(reloaded.keywords, ["moe"])
        self.assertEqual(reloaded.digest_frequency, "weekly")


class SetFrequencyCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "overlay.json")
        self.store = ProfileStore(self.path)
        self.cmd = SetFrequencyCommand()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_name(self) -> None:
        self.assertEqual(self.cmd.name, "set_frequency")

    def test_empty_args_shows_current_and_options(self) -> None:
        reply = self.cmd.handle("", self.store)
        # Mentions the current canonical value and all three options.
        self.assertIn("2x_daily", reply)
        self.assertIn("daily", reply)
        self.assertIn("weekly", reply)
        # Reflects the actual current value after a change.
        self.store.set_digest_frequency("weekly")
        self.assertIn("weekly", self.cmd.handle("", self.store))

    def test_synonyms_map_to_canonical_and_persist(self) -> None:
        cases = {
            "2x_daily": ["2x", "twice", "2", "2xday", "due", "2X_DAILY"],
            "daily": ["daily", "1x", "1", "day", "giorno", "DAILY"],
            "weekly": ["weekly", "week", "7", "settimana", "sett", "Weekly"],
        }
        for canonical, synonyms in cases.items():
            for syn in synonyms:
                # Reset to a different value so each assertion is meaningful.
                self.store.set_digest_frequency(
                    "daily" if canonical != "daily" else "weekly"
                )
                reply = self.cmd.handle(syn, self.store)
                self.assertEqual(
                    self.store.digest_frequency, canonical,
                    msg=f"synonym {syn!r} should map to {canonical!r}",
                )
                self.assertIn("✅", reply)
                self.assertIn(canonical, reply)

    def test_synonym_with_surrounding_whitespace(self) -> None:
        self.cmd.handle("   weekly  ", self.store)
        self.assertEqual(self.store.digest_frequency, "weekly")

    def test_unrecognized_input_shows_options_and_does_not_raise(self) -> None:
        before = self.store.digest_frequency
        reply = self.cmd.handle("hourly", self.store)
        self.assertIn("2x_daily", reply)
        self.assertIn("daily", reply)
        self.assertIn("weekly", reply)
        # Stored value untouched on unrecognized input.
        self.assertEqual(self.store.digest_frequency, before)

    def test_persistence_through_dispatch_style_call(self) -> None:
        self.cmd.handle("weekly", self.store)
        # A fresh store over the same path sees the persisted choice.
        self.assertEqual(ProfileStore(self.path).digest_frequency, "weekly")


if __name__ == "__main__":
    unittest.main()
