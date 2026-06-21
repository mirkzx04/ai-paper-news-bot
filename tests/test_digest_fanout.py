"""Tests for run_digest_for_all — the multi-user delivery orchestration.

The pipeline internals (SPECTER, arXiv) are out of scope here: we patch
``app.run_digest_once`` and assert the FAN-OUT contract — per-user iteration,
per-user chat targeting, failure isolation, and blocked-user handling.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app  # noqa: E402
from src.telegram_api import PermanentSendError  # noqa: E402


class _FakeRegistry:
    def __init__(self, users) -> None:
        self._users = users
        self.blocked: list[str] = []

    def active_users(self):
        return list(self._users)

    def set_status(self, user_id, status):
        self.blocked.append((user_id, status))

    def count(self):
        return len(self._users)


class _FakeRunSummary:
    pass


class DigestFanoutTest(unittest.TestCase):
    def setUp(self) -> None:
        # No telegram env -> the owner aggregate push is a no-op (we don't assert it).
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        self.registry = _FakeRegistry([
            {"user_id": "u_a", "chat_id": 1001},
            {"user_id": "u_b", "chat_id": 1002},
            {"user_id": "u_c", "chat_id": 1003},
        ])

    def _run(self):
        return app.run_digest_for_all(
            cfg=mock.Mock(), store=mock.Mock(), registry=self.registry,
            preference_dataset=mock.Mock(), now="NOW", notifier_kind="console")

    def test_calls_once_per_active_user_with_their_chat(self) -> None:
        with mock.patch.object(app, "run_digest_once", return_value=_FakeRunSummary()) as m, \
             mock.patch.object(app, "apply_profile_overlay", side_effect=lambda c, p: c), \
             mock.patch.object(app, "enrich_author_ids", side_effect=lambda p: p), \
             mock.patch.object(app, "replace", side_effect=lambda c, **k: c), \
             mock.patch.object(app, "UserProfileStoreProvider"):
            results = self._run()
        self.assertEqual(len(results), 3)
        chat_ids = {c.kwargs["chat_id"] for c in m.call_args_list}
        self.assertEqual(chat_ids, {"1001", "1002", "1003"})
        for c in m.call_args_list:
            self.assertEqual(c.kwargs["notify_owner"], False)   # owner push aggregated

    def test_one_user_failure_does_not_abort_others(self) -> None:
        def side_effect(*a, **k):
            if k["user_id"] == "u_b":
                raise RuntimeError("boom")
            return _FakeRunSummary()
        with mock.patch.object(app, "run_digest_once", side_effect=side_effect), \
             mock.patch.object(app, "apply_profile_overlay", side_effect=lambda c, p: c), \
             mock.patch.object(app, "enrich_author_ids", side_effect=lambda p: p), \
             mock.patch.object(app, "replace", side_effect=lambda c, **k: c), \
             mock.patch.object(app, "UserProfileStoreProvider"):
            results = self._run()
        self.assertEqual(len(results), 3)                       # all three attempted

    def test_blocked_user_marked_in_registry(self) -> None:
        def side_effect(*a, **k):
            if k["user_id"] == "u_c":
                raise PermanentSendError(403, k["chat_id"], "blocked")
            return _FakeRunSummary()
        with mock.patch.object(app, "run_digest_once", side_effect=side_effect), \
             mock.patch.object(app, "apply_profile_overlay", side_effect=lambda c, p: c), \
             mock.patch.object(app, "enrich_author_ids", side_effect=lambda p: p), \
             mock.patch.object(app, "replace", side_effect=lambda c, **k: c), \
             mock.patch.object(app, "UserProfileStoreProvider"):
            self._run()
        self.assertIn(("u_c", "blocked"), self.registry.blocked)


if __name__ == "__main__":
    unittest.main()
