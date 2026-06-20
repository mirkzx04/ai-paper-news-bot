"""Multi-user profile isolation and anonymous preference attribution."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import telegram_poller as tp_mod  # noqa: E402
from src.commands.add_keywords import AddKeywordsCommand  # noqa: E402
from src.commands.dispatch import CommandDispatcher  # noqa: E402
from src.store.preference_dataset import PreferenceDataset, ProfileListener  # noqa: E402
from src.store.profile_store import ProfileStore, UserProfileStoreProvider  # noqa: E402
from src.telegram_poller import TelegramPoller  # noqa: E402


class _MetaStore:
    def __init__(self) -> None:
        self._meta: dict[str, str] = {}

    def get_meta(self, key):
        return self._meta.get(key)

    def set_meta(self, key, value):
        self._meta[key] = value


def _message(update_id: int, telegram_id: int, username: str, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1000 + update_id,
            "from": {"id": telegram_id, "username": username, "first_name": username.title()},
            "chat": {"id": telegram_id},
            "text": text,
        },
    }


class MultiUserPollerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.overlay = os.path.join(self.tmp.name, "profile_overlay.json")
        self.preferences_path = os.path.join(self.tmp.name, "preferences.jsonl")
        self.ds = PreferenceDataset(self.preferences_path)
        self.default_profile = ProfileStore(self.overlay)
        self.provider = UserProfileStoreProvider(
            self.overlay,
            listener_factory=lambda user_id: ProfileListener(self.ds, user_id=user_id),
        )
        self.sent: list[dict] = []
        self.updates: list[dict] = []

        self._orig_get = tp_mod.get_updates
        self._orig_send = tp_mod.send_message
        tp_mod.get_updates = lambda *a, **k: {"ok": True, "result": self.updates}
        tp_mod.send_message = lambda token, chat_id, text, parse_mode=None, timeout=20: (
            self.sent.append({"chat_id": chat_id, "text": text}) or None
        )

    def tearDown(self) -> None:
        tp_mod.get_updates = self._orig_get
        tp_mod.send_message = self._orig_send
        self.tmp.cleanup()

    def _poller(self) -> TelegramPoller:
        dispatcher = CommandDispatcher([AddKeywordsCommand()], self.default_profile)
        return TelegramPoller(
            "TOK", dispatcher, _MetaStore(),
            preference_dataset=self.ds,
            profile_store_provider=self.provider,
            user_id_resolver=lambda sender: f"u_{sender['id']}",
        )

    def test_keywords_are_saved_in_separate_anonymous_profiles(self) -> None:
        poller = self._poller()
        self.updates = [
            _message(1, 101, "alice_nick", "/add_keywords mechanistic interpretability"),
            _message(2, 202, "bob_nick", "/add_keywords graph transformers"),
        ]

        poller.poll_once()

        alice = ProfileStore(self.provider.path_for("u_101"))
        bob = ProfileStore(self.provider.path_for("u_202"))
        self.assertEqual(alice.keywords, ["mechanistic interpretability"])
        self.assertEqual(bob.keywords, ["graph transformers"])
        self.assertEqual(self.default_profile.keywords, [])

        events = self.ds.events()
        self.assertEqual([e["user_id"] for e in events], ["u_101", "u_202"])
        with open(self.preferences_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("alice_nick", raw)
        self.assertNotIn("bob_nick", raw)
        self.assertNotIn("Alice_Nick", raw)

    def test_event_filter_returns_one_anonymous_user(self) -> None:
        self.ds.log({"type": "vote", "user_id": "u_101", "canonical_key": "a", "signal": "up"})
        self.ds.log({"type": "vote", "user_id": "u_202", "canonical_key": "b", "signal": "down"})

        self.assertEqual(
            [e["canonical_key"] for e in self.ds.events(types=["vote"], user_id="u_101")],
            ["a"],
        )


if __name__ == "__main__":
    unittest.main()
