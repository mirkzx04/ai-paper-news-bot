"""Tests for `TelegramNotifier`, focused on HTTP 429 (rate-limit) handling.

`_send` must, on HTTP 429, read Telegram's ``retry_after`` from
``{"parameters": {"retry_after": <seconds>}}``, sleep for that long (capped) and
re-send, up to ``max_retries`` total attempts — instead of dropping the message
silently. A 200 first try is unchanged; an unparsable ``retry_after`` falls back
to a prudent default without crashing.

We monkeypatch the `requests` the module imported (capturing each POST) and patch
`time.sleep` so neither the per-message throttle nor the 429 backoff sleeps for
real. Stdlib `unittest` + `unittest.mock` only (pytest is not installed).
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.item import Item  # noqa: E402
from src.notify import telegram_notifier as tn  # noqa: E402
from src.notify.base import ScoredItem  # noqa: E402
from src.scoring.base import ScoreResult  # noqa: E402


class _FakeResp:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, status_code=200, payload=None, *, raise_on_json=False, text="") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True, "result": {"message_id": 1}}
        self._raise_on_json = raise_on_json
        self.text = text

    def json(self):
        if self._raise_on_json:
            raise ValueError("no json")
        return self._payload


class _RequestsStub:
    """Captures POSTs and returns a *queue* of responses (one per attempt).

    Mimics the slice of the `requests` module the notifier touches: `.post(...)`
    and the `.RequestException` attribute referenced in the `except` clause.
    """

    def __init__(self, responses) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.RequestException = Exception  # so `except requests.RequestException` works

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _rate_limited(retry_after):
    """A 429 response carrying the given retry_after in the standard body."""
    return _FakeResp(429, {"ok": False, "parameters": {"retry_after": retry_after}},
                     text="Too Many Requests")


def _make_item() -> Item:
    return Item(
        source="arxiv",
        external_id="2401.00001",
        title="A Test Paper",
        summary="An abstract.",
        url="https://arxiv.org/abs/2401.00001",
        published=datetime(2024, 1, 1, tzinfo=timezone.utc),
        authors=("Ada Lovelace",),
    )


def _make_scored() -> ScoredItem:
    return ScoredItem(item=_make_item(), result=ScoreResult(total=0.9, breakdown={"kw": 0.9}))


class SendRetryOn429Test(unittest.TestCase):
    """Direct unit tests of `_send` under rate limiting (with `time.sleep` mocked)."""

    def setUp(self) -> None:
        self._orig_requests = tn.requests
        # Patch time.sleep at the module level so the backoff never really sleeps.
        self._sleep_patcher = mock.patch.object(tn.time, "sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self) -> None:
        tn.requests = self._orig_requests
        self._sleep_patcher.stop()

    def _notifier(self, **kwargs):
        return tn.TelegramNotifier("TOK", "123", **kwargs)

    def test_429_then_200_retries_and_returns_result(self) -> None:
        # (a) 429 with retry_after, then a 200 -> notifier retries and returns the result dict.
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 42}})
        tn.requests = _RequestsStub([_rate_limited(3), ok])
        result = self._notifier(max_retries=3)._send("hi")
        self.assertEqual(result, {"message_id": 42})
        self.assertEqual(len(tn.requests.calls), 2)          # one retry happened
        self.mock_sleep.assert_called_once_with(3.0)         # (e) slept for retry_after

    def test_429_exhausts_retries_returns_none_without_raising(self) -> None:
        # (b) Repeated 429 beyond max_retries -> None, no exception.
        stub = _RequestsStub([_rate_limited(1) for _ in range(3)])
        tn.requests = stub
        result = self._notifier(max_retries=3)._send("hi")
        self.assertIsNone(result)
        self.assertEqual(len(stub.calls), 3)                 # exactly max_retries attempts
        self.assertEqual(self.mock_sleep.call_count, 2)      # slept between the 3 attempts

    def test_200_first_try_unchanged(self) -> None:
        # (c) 200 on the first attempt -> returns result, no sleep, single POST.
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 7}})
        tn.requests = _RequestsStub([ok])
        result = self._notifier()._send("hi")
        self.assertEqual(result, {"message_id": 7})
        self.assertEqual(len(tn.requests.calls), 1)
        self.mock_sleep.assert_not_called()

    def test_retry_after_missing_uses_default(self) -> None:
        # (d) 429 with NO retry_after -> fall back to the prudent default, no crash.
        no_ra = _FakeResp(429, {"ok": False, "parameters": {}}, text="Too Many Requests")
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 9}})
        tn.requests = _RequestsStub([no_ra, ok])
        result = self._notifier(max_retries=2)._send("hi")
        self.assertEqual(result, {"message_id": 9})
        self.mock_sleep.assert_called_once_with(tn._FALLBACK_RETRY_AFTER)

    def test_retry_after_non_numeric_uses_default(self) -> None:
        # (d) retry_after present but garbage -> default, no crash.
        bad_ra = _FakeResp(429, {"ok": False, "parameters": {"retry_after": "soon"}})
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 11}})
        tn.requests = _RequestsStub([bad_ra, ok])
        result = self._notifier(max_retries=2)._send("hi")
        self.assertEqual(result, {"message_id": 11})
        self.mock_sleep.assert_called_once_with(tn._FALLBACK_RETRY_AFTER)

    def test_429_body_not_json_uses_default(self) -> None:
        # A 429 whose body doesn't parse as JSON must still back off (default), not raise.
        bad_json = _FakeResp(429, raise_on_json=True, text="Too Many Requests")
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 13}})
        tn.requests = _RequestsStub([bad_json, ok])
        result = self._notifier(max_retries=2)._send("hi")
        self.assertEqual(result, {"message_id": 13})
        self.mock_sleep.assert_called_once_with(tn._FALLBACK_RETRY_AFTER)

    def test_retry_after_capped(self) -> None:
        # A hostile retry_after is clamped to retry_after_cap, never honoured literally.
        huge = _FakeResp(429, {"ok": False, "parameters": {"retry_after": 99999}})
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 15}})
        tn.requests = _RequestsStub([huge, ok])
        result = self._notifier(max_retries=2, retry_after_cap=30.0)._send("hi")
        self.assertEqual(result, {"message_id": 15})
        self.mock_sleep.assert_called_once_with(30.0)

    def test_other_non_200_not_retried(self) -> None:
        # A non-429 error (e.g. 400) keeps the old behaviour: single attempt, None, no sleep.
        bad = _FakeResp(400, {"ok": False}, text="Bad Request")
        stub = _RequestsStub([bad])
        tn.requests = stub
        result = self._notifier(max_retries=3)._send("hi")
        self.assertIsNone(result)
        self.assertEqual(len(stub.calls), 1)
        self.mock_sleep.assert_not_called()

    def test_403_raises_permanent_send_error(self) -> None:
        # A 403 (user blocked the bot / chat gone) is PERMANENT: raise so the
        # fan-out can mark the user blocked, instead of returning None.
        forbidden = _FakeResp(403, {"ok": False}, text="Forbidden: bot was blocked by the user")
        tn.requests = _RequestsStub([forbidden])
        notifier = self._notifier()
        with self.assertRaises(tn.PermanentSendError) as ctx:
            notifier._send("hi")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.chat_id, "123")


class NotifyThrottleTest(unittest.TestCase):
    """The default throttle is 1.0s and `notify` sleeps it between messages."""

    def setUp(self) -> None:
        self._orig_requests = tn.requests
        self._sleep_patcher = mock.patch.object(tn.time, "sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self) -> None:
        tn.requests = self._orig_requests
        self._sleep_patcher.stop()

    def test_default_throttle_is_one_second(self) -> None:
        self.assertEqual(tn.TelegramNotifier("TOK", "123").throttle, 1.0)

    def test_notify_sleeps_throttle_between_messages(self) -> None:
        ok = _FakeResp(200, {"ok": True, "result": {"message_id": 1}})
        # Enough 200s for two messages.
        tn.requests = _RequestsStub([ok, ok])
        notifier = tn.TelegramNotifier("TOK", "123")  # default throttle=1.0
        notifier.notify([_make_scored(), _make_scored()], kind="digest")
        # Two messages -> two throttle sleeps, each at the 1.0s default.
        self.assertEqual(self.mock_sleep.call_count, 2)
        for call in self.mock_sleep.call_args_list:
            self.assertEqual(call.args, (1.0,))


if __name__ == "__main__":
    unittest.main()
