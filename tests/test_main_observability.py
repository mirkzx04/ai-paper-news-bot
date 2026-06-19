"""Tests for the digest-mode observability helpers.

Covers the two module-level helpers in isolation (no pipeline, no network):
  - `_heartbeat_text` formats a RunSummary into the one-line owner heartbeat;
  - `_admin_push` is best-effort and never raises, even when the underlying
    `send_message` blows up (a failed push must not mask the run / cleanup).

These helpers were extracted from main.py into src/app.py (the shared wiring
module reused by both the CLI and the future serve loop); the tests now target
`app` directly, where they live and where `send_message` is imported.

Stdlib unittest only (no extra deps); pytest can also collect these.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app  # noqa: E402
from src.pipeline import RunSummary  # noqa: E402


def _summary(**overrides) -> RunSummary:
    # digest_total mirrors digest by default (no cap effect when both are 0);
    # added so the extended RunSummary contract constructs cleanly here too.
    base = dict(fetched=0, unique=0, fresh=0, relevant=0,
                alerts=0, digest=0, digest_total=0, scoring_errors=0)
    base.update(overrides)
    return RunSummary(**base)


class HeartbeatTextTest(unittest.TestCase):
    def test_reports_alerts_digest_fresh_and_scoring_errors(self) -> None:
        text = app._heartbeat_text(
            _summary(alerts=3, digest=7, fresh=42, scoring_errors=2))
        # The owner must be able to read all four load-bearing counts at a glance.
        self.assertIn("3 alert", text)
        self.assertIn("7 digest", text)
        self.assertIn("42 nuovi", text)
        self.assertIn("2 scoring-error", text)

    def test_zero_run_is_still_a_valid_heartbeat(self) -> None:
        # A quiet run (nothing fetched/sent) must still produce a sane line so
        # the owner knows the cron is alive rather than silent.
        text = app._heartbeat_text(_summary())
        self.assertTrue(text.startswith("✅ digest:"))
        self.assertIn("0 alert", text)
        self.assertIn("0 scoring-error", text)


class AdminPushTest(unittest.TestCase):
    def test_passes_through_to_send_message_on_happy_path(self) -> None:
        with mock.patch.object(app, "send_message") as send:
            app._admin_push("TOKEN", "CHAT", "hello")
        send.assert_called_once_with("TOKEN", "CHAT", "hello")

    def test_does_not_raise_when_send_message_fails(self) -> None:
        # Network blip / 4xx surfacing as an exception inside send_message must
        # be swallowed: the push is observability, not the job.
        with mock.patch.object(app, "send_message",
                               side_effect=RuntimeError("boom")):
            try:
                app._admin_push("TOKEN", "CHAT", "hello")
            except Exception as exc:  # pragma: no cover - this is the failure we guard against
                self.fail(f"_admin_push must never raise, got {exc!r}")


if __name__ == "__main__":
    unittest.main()
