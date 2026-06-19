"""Tests for `PreferenceDataset` — append-only JSONL preference log.

Covers: append + read round-trip, the auto-stamped `ts`/`type`, defensive
handling of a missing file and of a corrupt file/line, type filtering, and the
`ProfileListener` -> event translation. Pure stdlib `unittest` (no pytest dep);
run with `python -m unittest`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store.preference_dataset import PreferenceDataset, ProfileListener


class PreferenceDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "preferences.jsonl")
        self.ds = PreferenceDataset(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_reads_empty(self) -> None:
        # No file written yet: reads must be empty, never raise.
        self.assertEqual(self.ds.events(), [])
        self.assertEqual(self.ds.count(), 0)

    def test_log_appends_and_stamps_ts_and_type(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "author", "value": "Neel Nanda"})
        events = self.ds.events()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["type"], "profile_add")
        self.assertEqual(ev["kind"], "author")
        self.assertEqual(ev["value"], "Neel Nanda")
        # ts is auto-added, UTC ISO-8601 (offset-aware).
        self.assertIn("ts", ev)
        from datetime import datetime
        parsed = datetime.fromisoformat(ev["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_log_is_append_only(self) -> None:
        for i in range(3):
            self.ds.log({"type": "profile_add", "kind": "keyword", "value": f"kw{i}"})
        self.assertEqual(self.ds.count(), 3)
        # File holds exactly one JSON object per line (true JSONL).
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        for ln in lines:
            json.loads(ln)  # each line independently parseable

    def test_log_does_not_mutate_caller_dict(self) -> None:
        event = {"type": "profile_add", "kind": "seed", "value": "2101.03961"}
        self.ds.log(event)
        # `ts` must NOT leak back into the caller's dict.
        self.assertNotIn("ts", event)

    def test_type_filtering(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "author", "value": "A"})
        self.ds.log({"type": "profile_remove", "kind": "author", "value": "A"})
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "k"})
        self.assertEqual(len(self.ds.events()), 3)
        self.assertEqual(len(self.ds.events(types=["profile_add"])), 1)
        self.assertEqual(
            len(self.ds.events(types=["profile_add", "profile_remove"])), 2
        )
        self.assertEqual(self.ds.events(types=["vote"])[0]["signal"], "up")
        self.assertEqual(self.ds.events(types=["nonexistent"]), [])

    def test_missing_type_defaults_to_unknown(self) -> None:
        self.ds.log({"kind": "keyword", "value": "moe"})
        self.assertEqual(self.ds.events()[0]["type"], "unknown")

    def test_corrupt_line_is_skipped_not_fatal(self) -> None:
        self.ds.log({"type": "profile_add", "kind": "author", "value": "A"})
        # Inject a corrupt line between two good ones.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
            fh.write("\n")  # blank line too
        self.ds.log({"type": "profile_add", "kind": "author", "value": "B"})
        events = self.ds.events()
        self.assertEqual(len(events), 2)
        self.assertEqual([e["value"] for e in events], ["A", "B"])

    def test_non_dict_json_line_is_skipped(self) -> None:
        # `log()` (which creates the parent dir) isn't used here; create it.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps([1, 2, 3]) + "\n")          # a list, not an object
            fh.write(json.dumps("a bare string") + "\n")    # a string
            fh.write(json.dumps({"type": "vote", "signal": "down"}) + "\n")
        events = self.ds.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "vote")

    def test_log_never_raises_on_bad_payload(self) -> None:
        # A non-serialisable value must be swallowed, not raised.
        class NotSerialisable:
            pass

        # Should not raise.
        self.ds.log({"type": "vote", "obj": NotSerialisable()})
        # And it must not have written a broken line either.
        self.assertEqual(self.ds.count(), 0)


class ProfileListenerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = PreferenceDataset(os.path.join(self.tmp.name, "preferences.jsonl"))
        self.listener = ProfileListener(self.ds)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_translates_to_profile_add(self) -> None:
        self.listener("add", "author", "Chris Olah")
        ev = self.ds.events()[0]
        self.assertEqual(ev["type"], "profile_add")
        self.assertEqual(ev["kind"], "author")
        self.assertEqual(ev["value"], "Chris Olah")

    def test_remove_translates_to_profile_remove(self) -> None:
        self.listener("remove", "keyword", "moe")
        ev = self.ds.events()[0]
        self.assertEqual(ev["type"], "profile_remove")
        self.assertEqual(ev["kind"], "keyword")
        self.assertEqual(ev["value"], "moe")

    def test_unknown_action_is_preserved_not_dropped(self) -> None:
        self.listener("weird", "topic", "x")
        ev = self.ds.events()[0]
        self.assertEqual(ev["type"], "profile_weird")


if __name__ == "__main__":
    unittest.main()
