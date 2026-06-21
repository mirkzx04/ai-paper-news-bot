"""Tests for GDPR erasure (erase_user) across all four stores."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.privacy import erase_user  # noqa: E402
from src.store.preference_dataset import PreferenceDataset  # noqa: E402
from src.store.profile_store import UserProfileStoreProvider  # noqa: E402
from src.store.user_registry import UserRegistry  # noqa: E402


class EraseUserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.overlay = os.path.join(self.data, "profile_overlay.json")
        self.prefs = os.path.join(self.data, "preferences.jsonl")
        self.registry_path = os.path.join(self.data, "user_registry.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_user(self, reg: UserRegistry, uid: str, chat_id: int) -> str:
        provider = UserProfileStoreProvider(self.overlay)
        ps = provider.for_user(uid)
        ps.add_keywords(["diffusion"])               # creates data/users/<uid>/...
        user_dir = os.path.dirname(provider.path_for(uid))
        reg.register(uid, chat_id)
        return user_dir

    def test_erase_removes_everything_for_the_user_only(self) -> None:
        reg = UserRegistry(self.registry_path, secret=None)
        user_dir = self._seed_user(reg, "u_target", 111)
        self._seed_user(reg, "u_other", 222)
        ds = PreferenceDataset(self.prefs)
        ds.log({"type": "vote", "user_id": "u_target", "signal": "up", "canonical_key": "a"})
        ds.log({"type": "vote", "user_id": "u_other", "signal": "up", "canonical_key": "b"})

        self.assertTrue(os.path.isdir(user_dir))
        summary = erase_user("u_target", registry=reg,
                             base_overlay_path=self.overlay, preferences_path=self.prefs)

        # 1) per-user dir gone; 2) registry entry gone; 3) only their rows removed.
        self.assertFalse(os.path.isdir(user_dir))
        self.assertTrue(summary["user_dir"])
        self.assertTrue(summary["registry"])
        self.assertEqual(summary["preference_rows"], 1)
        self.assertIsNone(reg.get("u_target"))
        self.assertIsNotNone(reg.get("u_other"))

        remaining = [json.loads(l) for l in open(self.prefs, encoding="utf-8") if l.strip()]
        self.assertEqual([r["user_id"] for r in remaining], ["u_other"])

    def test_erase_unknown_user_is_safe(self) -> None:
        reg = UserRegistry(self.registry_path, secret=None)
        summary = erase_user("u_nope", registry=reg,
                             base_overlay_path=self.overlay, preferences_path=self.prefs)
        self.assertEqual(summary, {"user_dir": False, "registry": False, "preference_rows": 0})


if __name__ == "__main__":
    unittest.main()
