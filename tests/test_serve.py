"""Tests for the long-running serve loop (src/serve.py).

The loop is single-threaded: long-poll for commands/votes, then attempt a digest
(gated by cadence). We test the credential guard and one full cycle with
everything mocked — no SQLite, no network, no real signals. ``max_cycles`` bounds
the otherwise-infinite loop. Stdlib unittest only.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from src import app, serve


class ServeCredentialsTest(unittest.TestCase):
    def test_missing_token_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(serve, "load_env"):
            with self.assertRaises(app.MissingCredentialsError):
                serve.serve_forever()

    def test_missing_chat_id_raises(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"}, clear=True), \
             mock.patch.object(serve, "load_env"):
            with self.assertRaises(app.MissingCredentialsError):
                serve.serve_forever()


class ServeCycleTest(unittest.TestCase):
    def test_one_cycle_polls_then_digests_and_closes(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}
        poller, store, sent_items = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(serve, "load_env"), \
             mock.patch.object(serve, "SqliteStore", return_value=store), \
             mock.patch.object(serve, "SentItemsStore", return_value=sent_items), \
             mock.patch.object(serve, "ProfileStore"), \
             mock.patch.object(serve, "PreferenceDataset"), \
             mock.patch.object(serve, "ProfileFlow"), \
             mock.patch.object(serve, "ProfileListener"), \
             mock.patch.object(serve, "ReportLog"), \
             mock.patch.object(serve, "set_my_commands"), \
             mock.patch.object(serve.signal, "signal"), \
             mock.patch.object(serve, "load_config"), \
             mock.patch.object(serve, "apply_profile_overlay"), \
             mock.patch.object(serve, "replace"), \
             mock.patch.object(serve.app, "build_poller", return_value=poller), \
             mock.patch.object(serve.app, "build_commands", return_value=[]), \
             mock.patch.object(serve.app, "enrich_author_ids"), \
             mock.patch.object(serve.app, "run_digest_once") as run_digest:
            serve.serve_forever(max_cycles=1)

        poller.poll_once.assert_called_once_with(long_poll=serve._LONG_POLL_SECONDS)
        run_digest.assert_called_once()
        # the digest attempt is the telegram path, following the cadence
        self.assertEqual(run_digest.call_args.kwargs["notifier_kind"], "telegram")
        self.assertIsNone(run_digest.call_args.kwargs["lookback_override"])
        sent_items.close.assert_called_once()
        store.close.assert_called_once()

    def test_poll_failure_does_not_kill_the_loop(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}
        poller, store, sent_items = mock.Mock(), mock.Mock(), mock.Mock()
        poller.poll_once.side_effect = RuntimeError("network down")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(serve, "load_env"), \
             mock.patch.object(serve, "SqliteStore", return_value=store), \
             mock.patch.object(serve, "SentItemsStore", return_value=sent_items), \
             mock.patch.object(serve, "ProfileStore"), \
             mock.patch.object(serve, "PreferenceDataset"), \
             mock.patch.object(serve, "ProfileFlow"), \
             mock.patch.object(serve, "ProfileListener"), \
             mock.patch.object(serve, "ReportLog"), \
             mock.patch.object(serve, "set_my_commands"), \
             mock.patch.object(serve.signal, "signal"), \
             mock.patch.object(serve, "load_config"), \
             mock.patch.object(serve, "apply_profile_overlay"), \
             mock.patch.object(serve, "replace"), \
             mock.patch.object(serve.app, "build_poller", return_value=poller), \
             mock.patch.object(serve.app, "build_commands", return_value=[]), \
             mock.patch.object(serve.app, "enrich_author_ids"), \
             mock.patch.object(serve.app, "run_digest_once"):
            serve.serve_forever(max_cycles=1)  # must NOT raise

        store.close.assert_called_once()  # clean shutdown despite the poll error


if __name__ == "__main__":
    unittest.main()
