from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.user_identity import (  # noqa: E402
    WeakUserIdSecretError,
    anonymous_user_id,
    assert_strong_secret,
    secret_is_strong,
    telegram_user_id,
)


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


class SecretHardeningTest(unittest.TestCase):
    def test_strong_secret_detected(self) -> None:
        with mock.patch.dict(os.environ, {"USER_ID_SECRET": "z" * 40}, clear=True):
            self.assertTrue(secret_is_strong())

    def test_missing_short_or_dev_default_is_weak(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(secret_is_strong())                 # missing
        with mock.patch.dict(os.environ, {"USER_ID_SECRET": "short"}, clear=True):
            self.assertFalse(secret_is_strong())                 # too short
        with mock.patch.dict(os.environ, {"USER_ID_SECRET": "dev-only-user-id-secret"}, clear=True):
            self.assertFalse(secret_is_strong())                 # dev default
        # Falling back to the bot token does NOT count as a strong dedicated secret.
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t" * 40}, clear=True):
            self.assertFalse(secret_is_strong())

    def test_assert_requires_in_production(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_ENV": "production"}, clear=True):
            with self.assertRaises(WeakUserIdSecretError):
                assert_strong_secret()

    def test_assert_warns_only_in_dev(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert_strong_secret()                               # must not raise

    def test_assert_passes_with_strong_secret_even_in_prod(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_ENV": "production", "USER_ID_SECRET": "z" * 40}, clear=True):
            assert_strong_secret()                               # must not raise


if __name__ == "__main__":
    unittest.main()
