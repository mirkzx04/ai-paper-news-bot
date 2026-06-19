"""Tests for the owner-only poller commands (/reports, /errors), the admin
restriction (admin vs non-admin), N parsing, and the end-of-run error push.

We monkeypatch the telegram_api functions that telegram_poller imported by name
(get_updates / send_message / delete_message) so nothing hits the network, and
capture every outgoing sendMessage call.

Stdlib unittest only (no extra deps); pytest can also collect these.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import telegram_poller as tp_mod  # noqa: E402
from src.error_log import ErrorLog  # noqa: E402
from src.report_log import ReportLog  # noqa: E402
from src.telegram_poller import TelegramPoller  # noqa: E402

ADMIN = "12345"
OTHER = "99999"


class _FakeStore:
    """Minimal in-memory Store: only the meta get/set the poller touches."""

    def __init__(self) -> None:
        self._meta: dict[str, str] = {}

    def get_meta(self, key):
        return self._meta.get(key)

    def set_meta(self, key, value):
        self._meta[key] = value

    # Unused by these tests but part of the interface.
    def is_seen(self, key):  # pragma: no cover
        return False

    def mark_seen(self, key, when):  # pragma: no cover
        pass

    def close(self):  # pragma: no cover
        pass


class _DispatcherSpy:
    """Stands in for CommandDispatcher; records what it was asked to dispatch."""

    def __init__(self, reply="DISPATCHED") -> None:
        self.reply = reply
        self.seen: list[str] = []

    def dispatch(self, text):
        self.seen.append(text)
        return self.reply


def _make_update(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"message_id": 1000 + update_id,
                        "chat": {"id": chat_id}, "text": text}}


class PollerAdminBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.reports_path = os.path.join(self._tmp.name, "reports.json")
        self.errors_path = os.path.join(self._tmp.name, "error_log.json")
        self.report_log = ReportLog(self.reports_path)
        self.error_log = ErrorLog(self.errors_path)

        # Capture every send_message; control get_updates per test.
        self.sent: list[dict] = []
        self._updates: list[dict] = []

        self._orig_get = tp_mod.get_updates
        self._orig_send = tp_mod.send_message
        self._orig_del = tp_mod.delete_message

        def fake_get_updates(token, offset=None, timeout=0, req_timeout=25):
            return {"ok": True, "result": self._updates}

        def fake_send_message(token, chat_id, text, parse_mode=None, timeout=20):
            self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
            return None

        def fake_delete_message(token, chat_id, message_id, timeout=10):
            return True

        tp_mod.get_updates = fake_get_updates
        tp_mod.send_message = fake_send_message
        tp_mod.delete_message = fake_delete_message

    def tearDown(self) -> None:
        tp_mod.get_updates = self._orig_get
        tp_mod.send_message = self._orig_send
        tp_mod.delete_message = self._orig_del
        self._tmp.cleanup()

    def _make_poller(self, dispatcher=None, admin=ADMIN):
        return TelegramPoller(
            token="T",
            dispatcher=dispatcher or _DispatcherSpy(),
            store=_FakeStore(),
            flow=None,
            error_log=self.error_log,
            report_log=self.report_log,
            admin_chat_id=admin,
        )


class AdminRestrictionTest(PollerAdminBase):
    def test_admin_reports_intercepted_not_dispatched(self) -> None:
        self.report_log.add("alpha")
        self.report_log.add("beta")
        spy = _DispatcherSpy()
        poller = self._make_poller(dispatcher=spy)
        self._updates = [_make_update(1, ADMIN, "/reports")]

        poller.poll_once()

        # Exactly one reply, HTML, contains the report bodies, never dispatched.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["chat_id"], ADMIN)
        self.assertEqual(self.sent[0]["parse_mode"], "HTML")
        self.assertIn("alpha", self.sent[0]["text"])
        self.assertIn("beta", self.sent[0]["text"])
        self.assertEqual(spy.seen, [])  # /reports never reached the dispatcher

    def test_non_admin_reports_falls_through_to_dispatcher(self) -> None:
        self.report_log.add("secret report")
        spy = _DispatcherSpy(reply="Unknown command: /reports")
        poller = self._make_poller(dispatcher=spy)
        self._updates = [_make_update(1, OTHER, "/reports")]

        poller.poll_once()

        # The non-admin gets only the dispatcher's reply; no report content leaks.
        self.assertEqual(spy.seen, ["/reports"])
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("secret report", self.sent[0]["text"])

    def test_non_admin_errors_falls_through_to_dispatcher(self) -> None:
        self.error_log.record(command="/x", args="", error="leaky error")
        spy = _DispatcherSpy(reply="Unknown command: /errors")
        poller = self._make_poller(dispatcher=spy)
        self._updates = [_make_update(1, OTHER, "/errors")]

        poller.poll_once()

        self.assertEqual(spy.seen, ["/errors"])
        self.assertNotIn("leaky error", self.sent[0]["text"])

    def test_admin_chat_id_int_matches_string_env(self) -> None:
        # Telegram delivers chat id as an int; admin_chat_id comes from env (str).
        self.report_log.add("hi")
        poller = self._make_poller(admin=ADMIN)
        self._updates = [_make_update(1, int(ADMIN), "/reports")]  # int chat id
        poller.poll_once()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("hi", self.sent[0]["text"])

    def test_no_admin_configured_disables_commands(self) -> None:
        self.report_log.add("hi")
        spy = _DispatcherSpy(reply="Unknown command: /reports")
        poller = self._make_poller(dispatcher=spy, admin=None)
        self._updates = [_make_update(1, ADMIN, "/reports")]
        poller.poll_once()
        # With no admin chat, even the owner's /reports falls through.
        self.assertEqual(spy.seen, ["/reports"])


class AdminNParsingTest(PollerAdminBase):
    def test_reports_n_limits_records_shown(self) -> None:
        for i in range(5):
            self.report_log.add(f"report-{i}")
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/reports 2")]
        poller.poll_once()
        text = self.sent[0]["text"]
        # Only the two newest are rendered.
        self.assertIn("report-3", text)
        self.assertIn("report-4", text)
        self.assertNotIn("report-0", text)
        self.assertNotIn("report-2", text)

    def test_reports_default_n_when_no_arg(self) -> None:
        # Default is _ADMIN_DEFAULT_N; add one more than that and check the oldest
        # is dropped.
        n_default = tp_mod._ADMIN_DEFAULT_N
        for i in range(n_default + 1):
            self.report_log.add(f"r{i}")
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/reports")]
        poller.poll_once()
        text = self.sent[0]["text"]
        self.assertNotIn("r0\n", text + "\n")  # oldest dropped
        self.assertIn(f"r{n_default}", text)   # newest kept

    def test_n_parsing_helper_edges(self) -> None:
        p = TelegramPoller._parse_admin_n
        self.assertEqual(p(""), tp_mod._ADMIN_DEFAULT_N)
        self.assertEqual(p("   "), tp_mod._ADMIN_DEFAULT_N)
        self.assertEqual(p("notanint"), tp_mod._ADMIN_DEFAULT_N)
        self.assertEqual(p("3"), 3)
        self.assertEqual(p("3 extra junk"), 3)
        self.assertEqual(p("0"), 1)            # clamped up to 1
        self.assertEqual(p("-5"), 1)           # clamped up to 1
        self.assertEqual(p("99999"), tp_mod._ADMIN_MAX_N)  # clamped to max

    def test_reports_botname_suffix_parsed(self) -> None:
        self.report_log.add("hello")
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/reports@my_bot 1")]
        poller.poll_once()
        self.assertIn("hello", self.sent[0]["text"])

    def test_errors_command_renders_fields(self) -> None:
        self.error_log.record(command="/boom", args="a", error="KaBoom",
                              traceback_str="Traceback...\nValueError: KaBoom")
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/errors")]
        poller.poll_once()
        text = self.sent[0]["text"]
        self.assertIn("/boom", text)
        self.assertIn("KaBoom", text)

    def test_empty_reports_message(self) -> None:
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/reports")]
        poller.poll_once()
        self.assertIn("No reports", self.sent[0]["text"])

    def test_empty_errors_message(self) -> None:
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/errors")]
        poller.poll_once()
        self.assertIn("No errors", self.sent[0]["text"])

    def test_traceback_tail_truncated(self) -> None:
        long_tb = "X" * 5000 + "\nFinalError: boom"
        self.error_log.record(command="/c", args="", error="e", traceback_str=long_tb)
        poller = self._make_poller()
        self._updates = [_make_update(1, ADMIN, "/errors")]
        poller.poll_once()
        text = self.sent[0]["text"]
        # The tail (the actual exception line) survives; the head is dropped.
        self.assertIn("FinalError: boom", text)
        self.assertLess(len(text), 4096)  # safely under Telegram's limit


class NewErrorsThisRunTest(PollerAdminBase):
    def test_baseline_excludes_preexisting_errors(self) -> None:
        # An error logged BEFORE the poller is built must not count as "this run".
        self.error_log.record(command="/old", args="", error="old")
        poller = self._make_poller()
        self.assertEqual(poller.new_errors_this_run(), 0)
        self.assertIsNone(poller.summarize_new_errors())

    def test_counts_only_errors_after_construction(self) -> None:
        self.error_log.record(command="/old", args="", error="old")
        poller = self._make_poller()
        self.error_log.record(command="/new1", args="", error="new1")
        self.error_log.record(command="/new2", args="", error="new2")
        self.assertEqual(poller.new_errors_this_run(), 2)
        summary = poller.summarize_new_errors()
        self.assertIsNotNone(summary)
        self.assertIn("2 error", summary)

    def test_notify_new_errors_sends_to_admin(self) -> None:
        poller = self._make_poller()
        self.error_log.record(command="/x", args="", error="boom")
        sent = poller.notify_new_errors()
        self.assertTrue(sent)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["chat_id"], ADMIN)
        self.assertEqual(self.sent[0]["parse_mode"], "HTML")
        self.assertIn("boom", self.sent[0]["text"])

    def test_notify_new_errors_noop_without_new_errors(self) -> None:
        poller = self._make_poller()
        self.assertFalse(poller.notify_new_errors())
        self.assertEqual(self.sent, [])

    def test_notify_new_errors_noop_without_admin(self) -> None:
        poller = self._make_poller(admin=None)
        self.error_log.record(command="/x", args="", error="boom")
        self.assertFalse(poller.notify_new_errors())
        self.assertEqual(self.sent, [])

    def test_push_preview_caps_and_counts_remainder(self) -> None:
        poller = self._make_poller()
        for i in range(5):
            self.error_log.record(command=f"/c{i}", args="", error=f"err{i}")
        summary = poller.summarize_new_errors()
        self.assertIn("5 error", summary)
        # Only _PUSH_ERROR_PREVIEW newest are inlined; the rest are summarized.
        self.assertIn("and 3 more", summary)


if __name__ == "__main__":
    unittest.main()
