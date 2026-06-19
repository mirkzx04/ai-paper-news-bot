"""Tests for long-poll support in TelegramPoller.poll_once / telegram_api.get_updates.

The serve loop needs Telegram long-polling: get_updates(timeout=N) blocks server-
side for up to N seconds waiting for an update, instead of returning immediately
(run-once / GitHub-Actions mode). These tests pin three things:

* `poll_once(long_poll=N>0)` passes ``timeout=N`` to get_updates (long-poll on)
  and an HTTP `req_timeout` that outlives the server hold (``N + 5``).
* `poll_once()` with the default (``long_poll=0``) is unchanged: ``timeout=0``
  is forwarded (immediate return), so every existing run-once caller is unaffected.
* The mode is otherwise transparent: updates fetched under long-poll are processed
  exactly as before, and a failing fetch (``ok:false`` OR a raised RequestException)
  yields 0 processed updates WITHOUT propagating — a transient error must never
  break the long-running loop.

Plus a unit check that `get_updates` itself bumps `req_timeout` above `timeout`
when long-polling (and leaves it untouched for the run-once default).

We monkeypatch the telegram_api names the poller imported (get_updates /
send_message) so nothing hits the network. Stdlib `unittest` only.
"""

from __future__ import annotations

import os
import sys
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import telegram_api as api  # noqa: E402
from src import telegram_poller as tp_mod  # noqa: E402
from src.telegram_poller import TelegramPoller  # noqa: E402


class _FakeStore:
    """Minimal in-memory Store: only the meta get/set the poller touches."""

    def __init__(self) -> None:
        self._meta: dict[str, str] = {}

    def get_meta(self, key):
        return self._meta.get(key)

    def set_meta(self, key, value):
        self._meta[key] = value

    def is_seen(self, key):  # pragma: no cover
        return False

    def mark_seen(self, key, when):  # pragma: no cover
        pass

    def close(self):  # pragma: no cover
        pass


class _DispatcherSpy:
    def __init__(self, reply="DISPATCHED") -> None:
        self.reply = reply
        self.seen: list[str] = []

    def dispatch(self, text):
        self.seen.append(text)
        return self.reply


def _msg_update(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"message_id": 1000 + update_id, "chat": {"id": chat_id}, "text": text}}


class _Recorder:
    """Stand-in for get_updates that records the kwargs each call received and
    returns a programmable result (or raises a programmed exception)."""

    def __init__(self, result=None, raises=None) -> None:
        self.result = result if result is not None else {"ok": True, "result": []}
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, token, offset=None, timeout=0, req_timeout=25):
        self.calls.append({"token": token, "offset": offset,
                           "timeout": timeout, "req_timeout": req_timeout})
        if self.raises is not None:
            raise self.raises
        return self.result


class PollerLongPollBase(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict] = []

        self._orig_get = tp_mod.get_updates
        self._orig_send = tp_mod.send_message

        def fake_send_message(token, chat_id, text, parse_mode=None,
                              reply_markup=None, timeout=20):
            self.sent.append({"chat_id": chat_id, "text": text})
            return None

        tp_mod.send_message = fake_send_message

    def tearDown(self) -> None:
        tp_mod.get_updates = self._orig_get
        tp_mod.send_message = self._orig_send

    def _make_poller(self, dispatcher=None, timeout=20):
        return TelegramPoller(
            token="T",
            dispatcher=dispatcher or _DispatcherSpy(),
            store=_FakeStore(),
            flow=None,
            timeout=timeout,
        )


class PollOnceLongPollTest(PollerLongPollBase):
    def test_long_poll_forwards_timeout_and_req_timeout(self) -> None:
        rec = _Recorder(result={"ok": True, "result": []})
        tp_mod.get_updates = rec
        poller = self._make_poller()

        poller.poll_once(long_poll=30)

        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        # Telegram long-poll seconds forwarded as-is...
        self.assertEqual(call["timeout"], 30)
        # ...and the HTTP read timeout outlives the server hold.
        self.assertEqual(call["req_timeout"], 35)
        self.assertGreater(call["req_timeout"], call["timeout"])

    def test_default_is_run_once_timeout_zero(self) -> None:
        rec = _Recorder(result={"ok": True, "result": []})
        tp_mod.get_updates = rec
        poller = self._make_poller(timeout=20)

        # No argument => default long_poll=0 => unchanged run-once behaviour.
        poller.poll_once()

        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call["timeout"], 0)             # immediate return
        self.assertEqual(call["req_timeout"], 25)        # self.timeout + 5, as before

    def test_explicit_zero_matches_default(self) -> None:
        rec = _Recorder(result={"ok": True, "result": []})
        tp_mod.get_updates = rec
        poller = self._make_poller(timeout=20)

        poller.poll_once(long_poll=0)

        call = rec.calls[0]
        self.assertEqual(call["timeout"], 0)
        self.assertEqual(call["req_timeout"], 25)

    def test_long_poll_still_processes_updates(self) -> None:
        # The mode must be transparent: updates returned under long-poll are
        # dispatched and the offset advances exactly as in run-once mode.
        spy = _DispatcherSpy(reply="ok")
        rec = _Recorder(result={"ok": True, "result": [_msg_update(5, 7, "/hello")]})
        tp_mod.get_updates = rec
        poller = self._make_poller(dispatcher=spy)

        sent = poller.poll_once(long_poll=30)

        self.assertEqual(sent, 1)
        self.assertEqual(spy.seen, ["/hello"])
        self.assertEqual(len(self.sent), 1)
        # Offset advanced to last_update_id + 1.
        self.assertEqual(poller.store.get_meta(tp_mod._OFFSET_KEY), "6")

    def test_long_poll_passes_offset(self) -> None:
        rec = _Recorder(result={"ok": True, "result": []})
        tp_mod.get_updates = rec
        poller = self._make_poller()
        poller.store.set_meta(tp_mod._OFFSET_KEY, "42")

        poller.poll_once(long_poll=30)

        self.assertEqual(rec.calls[0]["offset"], 42)


class PollOnceLongPollDefensiveTest(PollerLongPollBase):
    def test_ok_false_returns_zero_no_raise(self) -> None:
        rec = _Recorder(result={"ok": False, "description": "boom"})
        tp_mod.get_updates = rec
        poller = self._make_poller()

        # Must not raise; reports 0 processed.
        self.assertEqual(poller.poll_once(long_poll=30), 0)
        self.assertEqual(self.sent, [])

    def test_empty_result_returns_zero(self) -> None:
        # A long-poll that simply timed out server-side with no update.
        rec = _Recorder(result={"ok": True, "result": []})
        tp_mod.get_updates = rec
        poller = self._make_poller()
        self.assertEqual(poller.poll_once(long_poll=30), 0)

    def test_request_exception_swallowed_returns_zero(self) -> None:
        # The long-poll HTTP read timing out (or any transport error) must be
        # caught: the serve loop keeps going, the offset is left untouched.
        rec = _Recorder(raises=requests.exceptions.ReadTimeout("read timed out"))
        tp_mod.get_updates = rec
        poller = self._make_poller()
        poller.store.set_meta(tp_mod._OFFSET_KEY, "100")

        self.assertEqual(poller.poll_once(long_poll=30), 0)
        self.assertEqual(self.sent, [])
        # Offset unchanged => the next tick retries the same window.
        self.assertEqual(poller.store.get_meta(tp_mod._OFFSET_KEY), "100")

    def test_run_once_also_swallows_request_exception(self) -> None:
        # The defensive guard applies to the default mode too (it shares the path).
        rec = _Recorder(raises=requests.exceptions.ConnectionError("down"))
        tp_mod.get_updates = rec
        poller = self._make_poller()
        self.assertEqual(poller.poll_once(), 0)


class GetUpdatesReqTimeoutTest(unittest.TestCase):
    """Unit-level: get_updates clamps req_timeout above the long-poll timeout."""

    def setUp(self) -> None:
        self._orig_requests = api.requests
        self.captured: dict = {}

        class _Resp:
            @staticmethod
            def json():
                return {"ok": True, "result": []}

        class _FakeRequests:
            RequestException = requests.RequestException

            def get(_self, url, params=None, timeout=None):
                self.captured = {"url": url, "params": params, "timeout": timeout}
                return _Resp()

        api.requests = _FakeRequests()

    def tearDown(self) -> None:
        api.requests = self._orig_requests

    def test_longpoll_bumps_req_timeout_above_timeout(self) -> None:
        # req_timeout (25) < timeout (30): must be clamped up to timeout + 5.
        api.get_updates("T", timeout=30, req_timeout=25)
        self.assertEqual(self.captured["timeout"], 35)
        self.assertEqual(self.captured["params"]["timeout"], 30)

    def test_longpoll_keeps_larger_req_timeout(self) -> None:
        # A caller-supplied req_timeout already above timeout+5 is preserved.
        api.get_updates("T", timeout=30, req_timeout=100)
        self.assertEqual(self.captured["timeout"], 100)

    def test_run_once_leaves_req_timeout_untouched(self) -> None:
        # timeout=0 (default): no clamp, the HTTP timeout is exactly as passed.
        api.get_updates("T", timeout=0, req_timeout=25)
        self.assertEqual(self.captured["timeout"], 25)
        self.assertEqual(self.captured["params"]["timeout"], 0)


if __name__ == "__main__":
    unittest.main()
