from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.user_identity import anonymous_user_id, telegram_user_id  # noqa: E402


class AnonymousUserIdTest(unittest.TestCase):
    def test_stable_and_non_reversible_shape(self) -> None:
        uid1 = anonymous_user_id(123456, secret="secret-a")
        uid2 = anonymous_user_id("123456", secret="secret-a")
        self.assertEqual(uid1, uid2)
        self.assertTrue(uid1.startswith("u_"))
        self.assertNotIn("123456", uid1)

    def test_secret_changes_mapping(self) -> None:
        self.assertNotEqual(
            anonymous_user_id(123456, secret="secret-a"),
            anonymous_user_id(123456, secret="secret-b"),
        )

    def test_telegram_payload_ignores_names(self) -> None:
        with mock.patch.dict(os.environ, {"USER_ID_SECRET": "secret"}, clear=True):
            uid = telegram_user_id({
                "id": 42,
                "username": "alice_nickname",
                "first_name": "Alice",
                "last_name": "Example",
            })

        self.assertIsNotNone(uid)
        self.assertNotIn("alice", uid.lower())
        self.assertNotIn("Alice", uid)
        self.assertNotIn("42", uid)


if __name__ == "__main__":
    unittest.main()
