"""Tests for ReportLog readers (recent / count) — defensive against missing /
corrupt / non-list files, and correct ordering/slicing for `recent`.

Stdlib unittest only (no extra deps); pytest can also collect these.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Make the project root importable when run as a bare script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.report_log import ReportLog  # noqa: E402


class ReportLogReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "reports.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    # --- missing file -------------------------------------------------------
    def test_recent_missing_file_returns_empty(self) -> None:
        log = ReportLog(self.path)
        self.assertEqual(log.recent(10), [])
        self.assertEqual(log.count(), 0)

    # --- corrupt / wrong-shape ---------------------------------------------
    def test_recent_corrupt_file_returns_empty(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        log = ReportLog(self.path)
        self.assertEqual(log.recent(5), [])
        self.assertEqual(log.count(), 0)

    def test_non_list_payload_returns_empty(self) -> None:
        self._write({"report": "oops, an object not a list"})
        log = ReportLog(self.path)
        self.assertEqual(log.recent(5), [])
        self.assertEqual(log.count(), 0)

    # --- valid file: ordering, slicing, edge N -----------------------------
    def test_recent_returns_tail_newest_last(self) -> None:
        records = [{"timestamp": f"t{i}", "report": f"r{i}"} for i in range(5)]
        self._write(records)
        log = ReportLog(self.path)
        # recent(2) -> the last two records, preserving file order.
        self.assertEqual(log.recent(2), records[-2:])
        self.assertEqual(log.count(), 5)

    def test_recent_n_larger_than_log_returns_all(self) -> None:
        records = [{"timestamp": "t0", "report": "r0"}]
        self._write(records)
        log = ReportLog(self.path)
        self.assertEqual(log.recent(100), records)

    def test_recent_zero_or_negative_returns_empty(self) -> None:
        self._write([{"timestamp": "t0", "report": "r0"}])
        log = ReportLog(self.path)
        self.assertEqual(log.recent(0), [])
        self.assertEqual(log.recent(-3), [])

    # --- round-trip with the existing writer -------------------------------
    def test_add_then_read_roundtrip(self) -> None:
        log = ReportLog(self.path)
        log.add("first")
        log.add("second")
        self.assertEqual(log.count(), 2)
        recent = log.recent(1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["report"], "second")  # newest is last


if __name__ == "__main__":
    unittest.main()
