"""Tests for UserRegistry — encryption at rest + the privacy/dataset separation.

Critical privacy invariants verified here:
  * with a secret, the raw chat id is NEVER written in clear to the registry file;
  * a wrong/rotated key fails the integrity check (record treated as unreadable),
    it never returns a bogus chat id;
  * the routable chat id never lands in the preference dataset (training data).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store.preference_dataset import PreferenceDataset  # noqa: E402
from src.store.user_registry import STATUS_BLOCKED, UserRegistry  # noqa: E402


class UserRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "user_registry.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_get_roundtrip_clear_text(self) -> None:
        reg = UserRegistry(self.path, secret=None)
        reg.register("u_abc", 123456)
        self.assertEqual(reg.get("u_abc")["chat_id"], "123456")
        self.assertTrue(reg.is_active("u_abc"))
        # Reload from disk -> same value.
        self.assertEqual(UserRegistry(self.path, secret=None).get("u_abc")["chat_id"], "123456")

    def test_chat_id_encrypted_at_rest_and_recoverable(self) -> None:
        reg = UserRegistry(self.path, secret="x" * 40)
        reg.register("u_abc", 987654321)
        raw = Path(self.path).read_text(encoding="utf-8")
        self.assertNotIn("987654321", raw)          # not stored in clear
        self.assertIn("enc:v1:", raw)               # stored as an encrypted blob
        # A registry with the SAME secret recovers it.
        reg2 = UserRegistry(self.path, secret="x" * 40)
        self.assertEqual(reg2.get("u_abc")["chat_id"], "987654321")

    def test_wrong_key_does_not_return_chat_id(self) -> None:
        UserRegistry(self.path, secret="x" * 40).register("u_abc", 555)
        # A rotated/wrong key fails the integrity tag -> None, never a bogus id.
        self.assertIsNone(UserRegistry(self.path, secret="y" * 40).get("u_abc"))

    def test_status_and_active_users(self) -> None:
        reg = UserRegistry(self.path, secret=None)
        reg.register("u_a", 1)
        reg.register("u_b", 2)
        reg.set_status("u_b", STATUS_BLOCKED)
        actives = {e["user_id"] for e in reg.active_users()}
        self.assertEqual(actives, {"u_a"})

    def test_delete_removes_entry(self) -> None:
        reg = UserRegistry(self.path, secret=None)
        reg.register("u_a", 1)
        self.assertTrue(reg.delete("u_a"))
        self.assertIsNone(reg.get("u_a"))
        self.assertFalse(reg.delete("u_a"))         # already gone

    def test_chat_id_never_in_preference_dataset(self) -> None:
        # The dataset (RankNet training data) must hold only the anonymous id +
        # signals — never the routable chat id, which lives only in the registry.
        prefs_path = os.path.join(self.tmp.name, "preferences.jsonl")
        ds = PreferenceDataset(prefs_path)
        reg = UserRegistry(self.path, secret="x" * 40)
        reg.register("u_abc", 424242)
        ds.log({"type": "vote", "user_id": "u_abc", "signal": "up",
                "canonical_key": "arxiv:1"})
        body = Path(prefs_path).read_text(encoding="utf-8")
        self.assertIn("u_abc", body)                # anonymous id present
        self.assertNotIn("424242", body)            # raw chat id absent
        record = json.loads(body.splitlines()[0])
        self.assertNotIn("chat_id", record)


if __name__ == "__main__":
    unittest.main()
