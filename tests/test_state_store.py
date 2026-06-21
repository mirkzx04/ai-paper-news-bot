"""Tests for the state-store gist size guardrail."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.state_store as ss  # noqa: E402


class _RecordingRequests:
    def __init__(self) -> None:
        self.patched = False
        self.RequestException = Exception

    def patch(self, *a, **k):
        self.patched = True
        return mock.Mock(raise_for_status=lambda: None, status_code=200)


class StateStoreGuardrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("data", exist_ok=True)

    def tearDown(self) -> None:
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def _write_data(self, nbytes: int) -> None:
        with open(os.path.join("data", "blob.bin"), "wb") as fh:
            fh.write(os.urandom(nbytes))   # random => no gzip shrink

    def test_push_fails_when_state_exceeds_limit(self) -> None:
        self._write_data(50_000)
        req = _RecordingRequests()
        with mock.patch.object(ss, "requests", req), \
             mock.patch.object(ss, "_GIST_INLINE_LIMIT", 1000):
            with self.assertRaises(ss.StateTooLargeError):
                ss.push("gid", "tok")
        self.assertFalse(req.patched)       # never attempted the PATCH

    def test_push_succeeds_under_limit(self) -> None:
        self._write_data(100)
        req = _RecordingRequests()
        with mock.patch.object(ss, "requests", req), \
             mock.patch.object(ss, "_GIST_INLINE_LIMIT", 5_000_000):
            ss.push("gid", "tok")
        self.assertTrue(req.patched)        # PATCH attempted


if __name__ == "__main__":
    unittest.main()
