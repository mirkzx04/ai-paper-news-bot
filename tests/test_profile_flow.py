"""ProfileFlow: a slash-command mid-onboarding must not be eaten as step input.

Regression test for the dry-run bug where sending /start during the authors/
topics step saved "/start" as an author and a keyword (profile + dataset
pollution). Stdlib unittest only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from src.flow.profile_flow import ProfileFlow
from src.store.profile_store import ProfileStore

CHAT = 6486889333


class _FakeMetaStore:
    """In-memory stand-in for the Store's meta kv (flow state lives here)."""

    def __init__(self) -> None:
        self._m: dict = {}

    def get_meta(self, key):
        return self._m.get(key)

    def set_meta(self, key, value):
        self._m[key] = value


class CommandDuringFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeMetaStore()
        self.ps = ProfileStore(os.path.join(tempfile.mkdtemp(), "overlay.json"))
        # resolver never hits arXiv in tests
        self.flow = ProfileFlow(self.store, self.ps, resolver=lambda _t: (None, None))

    def test_command_in_authors_step_is_not_saved(self) -> None:
        self.store.set_meta(f"flow:{CHAT}", "authors")
        reply = self.flow.maybe_handle(CHAT, "/start")
        self.assertIsNone(reply)                       # handed to the dispatcher
        self.assertEqual(list(self.ps.authors), [])    # profile NOT polluted
        self.assertEqual(self.flow.active_step(CHAT), "authors")  # flow waits

    def test_command_in_topics_step_is_not_saved(self) -> None:
        self.store.set_meta(f"flow:{CHAT}", "topics")
        reply = self.flow.maybe_handle(CHAT, "/help")
        self.assertIsNone(reply)
        self.assertEqual(list(self.ps.keywords), [])
        self.assertEqual(self.flow.active_step(CHAT), "topics")

    def test_real_input_still_advances_and_saves(self) -> None:
        self.store.set_meta(f"flow:{CHAT}", "authors")
        reply = self.flow.maybe_handle(CHAT, "Neel Nanda")
        self.assertIsNotNone(reply)                    # flow handled it
        self.assertIn("neel nanda", [a.lower() for a in self.ps.authors])
        self.assertEqual(self.flow.active_step(CHAT), "topics")  # advanced

    def test_cancel_still_works(self) -> None:
        self.store.set_meta(f"flow:{CHAT}", "authors")
        reply = self.flow.maybe_handle(CHAT, "/annulla")
        self.assertEqual(reply, "Profile setup canceled.")
        self.assertIsNone(self.flow.active_step(CHAT))


if __name__ == "__main__":
    unittest.main()
