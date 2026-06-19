"""Tests for the shared wiring in src/app.py.

Focus on the two pieces of behaviour-bearing logic that the CLI (main.py) and
the future serve loop both depend on:

  * ``run_digest_once`` — the digest tick: the cadence gate (skip -> None), the
    happy path (send -> RunSummary + persisted ``last_digest_at`` + owner
    heartbeat), and the ``dry_run`` invariant (run, but do NOT persist state).
  * ``build_notifier`` — raises ``ValueError`` (not ``parser.error``) when the
    Telegram credentials are missing, so callers translate it to their own idiom.

The heavy pipeline construction (``app.build_pipeline`` -> SPECTER, network) is
monkeypatched to a tiny in-memory fake so these stay fast and offline; ``now`` is
injected so the cadence logic is deterministic. Stdlib unittest only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app  # noqa: E402
from src.pipeline import RunSummary  # noqa: E402


# --------------------------------------------------------------------------- #
# In-memory fakes (no SQLite, no network).
# --------------------------------------------------------------------------- #
class FakeStore:
    """Minimal Store stand-in: just the meta key/value pair run_digest_once uses.

    Records whether ``close()`` was called so the test can assert run_digest_once
    leaves the store OPEN (lifecycle belongs to the caller / serve loop). On the
    telegram path run_digest_once derives the SentItemsStore db file from the
    store's live SQLite connection (``_store_db_path`` -> ``PRAGMA database_list``);
    pass ``db_path`` to back the fake with a real connection so that path works.
    """

    def __init__(self, meta: dict | None = None, db_path: str | None = None) -> None:
        self._meta = dict(meta or {})
        self.closed = False
        self.conn = None
        if db_path is not None:
            import sqlite3
            self.conn = sqlite3.connect(db_path)

    def get_meta(self, key: str):
        return self._meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    def close(self) -> None:  # pragma: no cover - asserted via .closed, never expected
        self.closed = True


class FakeProfileStore:
    def __init__(self, digest_frequency: str = "2x_daily") -> None:
        self.digest_frequency = digest_frequency


class FakePipeline:
    """Captures the args ``run`` was called with and returns a canned summary."""

    def __init__(self, summary: RunSummary) -> None:
        self._summary = summary
        self.run_calls: list = []

    def run(self, since, *, mark_seen: bool) -> RunSummary:
        self.run_calls.append(SimpleNamespace(since=since, mark_seen=mark_seen))
        return self._summary


def _summary(**overrides) -> RunSummary:
    base = dict(fetched=0, unique=0, fresh=0, relevant=0,
                alerts=0, digest=0, digest_total=0, scoring_errors=0)
    base.update(overrides)
    return RunSummary(**base)


def _cfg() -> SimpleNamespace:
    # run_digest_once only touches cfg.topics (FieldClassifier) and cfg.sources
    # (first-run lookback fallback) on the console path; build_pipeline is faked.
    return SimpleNamespace(topics={}, sources={"arxiv": {"lookback_days": 2}})


_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


class RunDigestOnceSkipTest(unittest.TestCase):
    """The cadence gate: a not-due tick returns None and does nothing."""

    def test_returns_none_and_does_not_run_when_not_due(self) -> None:
        # daily frequency, already sent earlier *today* -> not due on this tick.
        last = _NOW - timedelta(hours=2)
        store = FakeStore({app._LAST_DIGEST_KEY: last.isoformat()})
        profile_store = FakeProfileStore("daily")

        with mock.patch.object(app, "build_pipeline") as build_pipeline:
            result = app.run_digest_once(
                _cfg(), store, profile_store, None,
                notifier_kind="console", lookback_override=None,
                dry_run=False, now=_NOW,
            )

        self.assertIsNone(result)
        build_pipeline.assert_not_called()          # no SPECTER on a no-send tick
        # meta untouched -> the original last_digest_at survives for next time.
        self.assertEqual(store.get_meta(app._LAST_DIGEST_KEY), last.isoformat())
        self.assertFalse(store.closed)              # caller owns the store

    def test_manual_override_bypasses_the_cadence_gate(self) -> None:
        # Same not-due state as above, but an explicit lookback always sends.
        last = _NOW - timedelta(hours=2)
        store = FakeStore({app._LAST_DIGEST_KEY: last.isoformat()})
        profile_store = FakeProfileStore("daily")
        pipe = FakePipeline(_summary(fresh=1))

        with mock.patch.object(app, "build_pipeline", return_value=pipe):
            result = app.run_digest_once(
                _cfg(), store, profile_store, None,
                notifier_kind="console", lookback_override=3,
                dry_run=False, now=_NOW,
            )

        self.assertIsInstance(result, RunSummary)
        self.assertEqual(len(pipe.run_calls), 1)
        # Manual lookback drives `since` = now - N days (not the last digest).
        self.assertEqual(pipe.run_calls[0].since, _NOW - timedelta(days=3))


class RunDigestOnceSendTest(unittest.TestCase):
    """The happy path: due tick runs the pipeline and persists state."""

    def test_due_tick_runs_persists_state_and_returns_summary(self) -> None:
        store = FakeStore()  # never sent -> always due
        profile_store = FakeProfileStore("2x_daily")
        pipe = FakePipeline(_summary(alerts=1, digest=2, fresh=5))

        with mock.patch.object(app, "build_pipeline", return_value=pipe):
            result = app.run_digest_once(
                _cfg(), store, profile_store, None,
                notifier_kind="console", lookback_override=None,
                dry_run=False, now=_NOW,
            )

        self.assertIsInstance(result, RunSummary)
        self.assertEqual(result.alerts, 1)
        self.assertEqual(result.digest, 2)
        # First run, no prior last_digest_at -> since falls back to the configured
        # arxiv lookback window (2 days here).
        self.assertEqual(pipe.run_calls[0].since, _NOW - timedelta(days=2))
        self.assertTrue(pipe.run_calls[0].mark_seen)   # not a dry run
        # State advanced to `now` so the next tick respects the cadence.
        self.assertEqual(store.get_meta(app._LAST_DIGEST_KEY), _NOW.isoformat())
        self.assertFalse(store.closed)                 # store stays open for caller

    def test_since_is_the_last_digest_on_a_normal_recurring_tick(self) -> None:
        last = _NOW - timedelta(days=4)
        store = FakeStore({app._LAST_DIGEST_KEY: last.isoformat()})
        profile_store = FakeProfileStore("2x_daily")  # due every tick
        pipe = FakePipeline(_summary())

        with mock.patch.object(app, "build_pipeline", return_value=pipe):
            app.run_digest_once(
                _cfg(), store, profile_store, None,
                notifier_kind="console", lookback_override=None,
                dry_run=False, now=_NOW,
            )

        # Dynamic lookback: fetch everything since the last digest, nothing missed.
        self.assertEqual(pipe.run_calls[0].since, last)

    def test_telegram_send_pushes_heartbeat_to_owner(self) -> None:
        summary = _summary(alerts=2, digest=3, fresh=9)
        pipe = FakePipeline(summary)
        profile_store = FakeProfileStore("2x_daily")

        # Telegram path: run_digest_once opens a SentItemsStore from the store's db
        # file (so a later 👍/👎 resolves back to its paper), so back the fake with
        # a real temp db. build_pipeline is faked (the real notifier is never used)
        # and send_message is mocked so nothing leaves the process.
        env = {"TELEGRAM_BOT_TOKEN": "TKN", "TELEGRAM_CHAT_ID": "42"}
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "bot.db")
            store = FakeStore(db_path=db)
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(app, "build_pipeline", return_value=pipe), \
                 mock.patch.object(app, "build_notifier", return_value=object()), \
                 mock.patch.object(app, "send_message") as send:
                result = app.run_digest_once(
                    _cfg(), store, profile_store, None,
                    notifier_kind="telegram", lookback_override=None,
                    dry_run=False, now=_NOW,
                )

            self.assertIs(result, summary)
            send.assert_called_once()
            token, chat_id, text = send.call_args.args
            self.assertEqual(token, "TKN")
            self.assertEqual(chat_id, "42")
            self.assertEqual(text, app._heartbeat_text(summary))
            store.conn.close()


class RunDigestOnceDryRunTest(unittest.TestCase):
    """dry_run runs the pipeline but must not mutate persisted state."""

    def test_dry_run_does_not_persist_last_digest_and_does_not_mark_seen(self) -> None:
        store = FakeStore()  # no prior state
        profile_store = FakeProfileStore("2x_daily")
        pipe = FakePipeline(_summary(fresh=3))

        with mock.patch.object(app, "build_pipeline", return_value=pipe):
            result = app.run_digest_once(
                _cfg(), store, profile_store, None,
                notifier_kind="console", lookback_override=None,
                dry_run=True, now=_NOW,
            )

        self.assertIsInstance(result, RunSummary)
        # The whole point of --dry-run: re-show next run, so neither the seen-set
        # (mark_seen=False) nor the cadence cursor (last_digest_at) moves.
        self.assertFalse(pipe.run_calls[0].mark_seen)
        self.assertIsNone(store.get_meta(app._LAST_DIGEST_KEY))


class RunDigestOnceFailureTest(unittest.TestCase):
    """On pipeline failure: record + push + re-raise; state NOT advanced."""

    def test_pipeline_failure_is_reraised_and_state_not_persisted(self) -> None:
        store = FakeStore()
        profile_store = FakeProfileStore("2x_daily")

        class Boom(FakePipeline):
            def run(self, since, *, mark_seen):
                raise RuntimeError("pipeline blew up")

        boom = Boom(_summary())
        with mock.patch.object(app, "build_pipeline", return_value=boom), \
             mock.patch.object(app, "ErrorLog") as error_log:
            with self.assertRaises(RuntimeError):
                app.run_digest_once(
                    _cfg(), store, profile_store, None,
                    notifier_kind="console", lookback_override=None,
                    dry_run=False, now=_NOW,
                )

        # The failure was persisted for /errors, and the cadence cursor did NOT
        # advance (so a retry next tick re-attempts the same window).
        error_log.return_value.record.assert_called_once()
        self.assertIsNone(store.get_meta(app._LAST_DIGEST_KEY))

    def test_value_error_from_pipeline_is_recorded_and_reraised_as_valueerror(self) -> None:
        # Regression guard for the credential-vs-run distinction: a bare ValueError
        # raised INSIDE pipeline.run must go through the record + re-raise path (it
        # is a run failure), NOT be mistaken for a missing-credentials error. It is
        # a plain ValueError, never a MissingCredentialsError.
        store = FakeStore()
        profile_store = FakeProfileStore("2x_daily")

        class BadValue(FakePipeline):
            def run(self, since, *, mark_seen):
                raise ValueError("a value problem mid-run")

        with mock.patch.object(app, "build_pipeline", return_value=BadValue(_summary())), \
             mock.patch.object(app, "ErrorLog") as error_log:
            with self.assertRaises(ValueError) as ctx:
                app.run_digest_once(
                    _cfg(), store, profile_store, None,
                    notifier_kind="console", lookback_override=None,
                    dry_run=False, now=_NOW,
                )

        self.assertNotIsInstance(ctx.exception, app.MissingCredentialsError)
        error_log.return_value.record.assert_called_once()


class BuildNotifierTest(unittest.TestCase):
    def test_raises_value_error_without_telegram_credentials(self) -> None:
        # The framework-free contract: missing creds -> ValueError (the CLI then
        # translates it to parser.error). Must NOT call sys.exit here. The concrete
        # type is MissingCredentialsError (a ValueError subtype) so the CLI can map
        # only THIS to parser.error, not a ValueError from inside pipeline.run.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                app.build_notifier("telegram", field_classifier=None)
        self.assertIsInstance(ctx.exception, app.MissingCredentialsError)
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_console_kind_needs_no_credentials(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            notifier = app.build_notifier("console", field_classifier=None)
        # A console notifier is always constructible (no tokens involved).
        self.assertIsNotNone(notifier)


if __name__ == "__main__":
    unittest.main()
