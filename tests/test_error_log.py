"""Tests for ErrorLog readers (recent / count) and that the refactored `record`
still appends correctly through the shared `_read` helper.

Stdlib unittest only (no extra deps); pytest can also collect these.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.error_log import ErrorLog  # noqa: E402


class ErrorLogReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "error_log.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    # --- missing file -------------------------------------------------------
    def test_missing_file_returns_empty(self) -> None:
        log = ErrorLog(self.path)
        self.assertEqual(log.recent(10), [])
        self.assertEqual(log.count(), 0)

    # --- corrupt / wrong-shape ---------------------------------------------
    def test_corrupt_file_returns_empty(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[broken")
        log = ErrorLog(self.path)
        self.assertEqual(log.recent(5), [])
        self.assertEqual(log.count(), 0)

    def test_non_list_payload_returns_empty(self) -> None:
        self._write({"error": "not a list"})
        log = ErrorLog(self.path)
        self.assertEqual(log.recent(5), [])
        self.assertEqual(log.count(), 0)

    # --- valid file: ordering, slicing, edge N -----------------------------
    def test_recent_returns_tail_newest_last(self) -> None:
        records = [
            {"timestamp": f"t{i}", "command": f"/c{i}", "args": "",
             "error": f"e{i}", "traceback": None}
            for i in range(4)
        ]
        self._write(records)
        log = ErrorLog(self.path)
        self.assertEqual(log.recent(2), records[-2:])
        self.assertEqual(log.count(), 4)

    def test_recent_zero_or_negative_returns_empty(self) -> None:
        self._write([{"timestamp": "t0", "command": "/c", "args": "",
                      "error": "e", "traceback": None}])
        log = ErrorLog(self.path)
        self.assertEqual(log.recent(0), [])
        self.assertEqual(log.recent(-1), [])

    # --- record() still works through the shared _read ---------------------
    def test_record_appends_and_reads_back(self) -> None:
        log = ErrorLog(self.path)
        log.record(command="/a", args="x", error="boom", traceback_str="TB")
        log.record(command="/b", args="y", error="bang")
        self.assertEqual(log.count(), 2)
        latest = log.recent(1)[0]
        self.assertEqual(latest["command"], "/b")
        self.assertEqual(latest["error"], "bang")
        self.assertIsNone(latest["traceback"])

    def test_record_writes_append_only_jsonl(self) -> None:
        log = ErrorLog(self.path)
        log.record(command="/a", args="x", error="boom")
        log.record(command="/b", args="y", error="bang")

        with open(self.path, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]

        self.assertEqual([line["command"] for line in lines], ["/a", "/b"])

    def test_record_over_corrupt_file_starts_fresh(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("garbage, not json")
        log = ErrorLog(self.path)
        log.record(command="/a", args="", error="e")
        # The corrupt line is ignored; the appended record is still readable.
        self.assertEqual(log.count(), 1)
        self.assertEqual(log.recent(1)[0]["command"], "/a")

    def test_corrupt_jsonl_line_does_not_hide_later_records(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not-json\n")
            fh.write(json.dumps({"timestamp": "t1", "command": "/ok",
                                 "args": "", "error": "e", "traceback": None}) + "\n")
        log = ErrorLog(self.path)
        self.assertEqual(log.count(), 1)
        self.assertEqual(log.recent(1)[0]["command"], "/ok")

    def test_default_reader_includes_legacy_json_history(self) -> None:
        old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        try:
            os.makedirs("data")
            with open(os.path.join("data", "error_log.json"), "w", encoding="utf-8") as fh:
                json.dump([{"timestamp": "t0", "command": "/old", "args": "",
                            "error": "old", "traceback": None}], fh)

            log = ErrorLog()
            log.record(command="/new", args="", error="new")

            self.assertEqual(log.count(), 2)
            self.assertEqual([rec["command"] for rec in log.recent(2)], ["/old", "/new"])
            self.assertTrue(os.path.exists(os.path.join("data", "error_log.jsonl")))
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
