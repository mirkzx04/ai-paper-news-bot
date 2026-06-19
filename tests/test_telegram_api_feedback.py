"""Tests for the feedback additions to `telegram_api`:

* `send_message` gains an optional `reply_markup` that is JSON-serialised into
  the payload, while omitting it keeps the payload byte-for-byte as before
  (backward-compat).
* `answer_callback_query` posts to answerCallbackQuery and is defensive (never
  raises; returns a bool).

We monkeypatch the `requests` the module imported and capture each POST. Stdlib
`unittest` only.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import telegram_api as api  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=200, payload=None, raise_on_json=False) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self._raise_on_json = raise_on_json
        self.text = ""

    def json(self):
        if self._raise_on_json:
            raise ValueError("no json")
        return self._payload


class _RequestsSpy:
    """Captures POSTs; returns a queued/last response. Mimics requests.post."""

    def __init__(self, resp=None) -> None:
        self.calls: list[dict] = []
        self.resp = resp or _FakeResp()
        self.RequestException = Exception  # so `except requests.RequestException` works

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class SendMessageReplyMarkupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = api.requests
        self.spy = _RequestsSpy()
        api.requests = self.spy

    def tearDown(self) -> None:
        api.requests = self._orig

    def test_no_reply_markup_payload_unchanged(self) -> None:
        api.send_message("TOK", 123, "hello")
        payload = self.spy.calls[0]["json"]
        # Exactly the original two keys — no reply_markup leaks in.
        self.assertEqual(payload, {"chat_id": 123, "text": "hello"})
        self.assertNotIn("reply_markup", payload)

    def test_parse_mode_still_supported(self) -> None:
        api.send_message("TOK", 123, "hi", parse_mode="HTML")
        self.assertEqual(self.spy.calls[0]["json"]["parse_mode"], "HTML")

    def test_reply_markup_is_json_serialised(self) -> None:
        markup = {"inline_keyboard": [[
            {"text": "👍", "callback_data": "fb:u:arxiv:2401.1"},
            {"text": "👎", "callback_data": "fb:d:arxiv:2401.1"},
        ]]}
        api.send_message("TOK", 123, "hi", reply_markup=markup)
        payload = self.spy.calls[0]["json"]
        # The Bot API needs reply_markup as a JSON *string* in the form field.
        self.assertIsInstance(payload["reply_markup"], str)
        self.assertEqual(json.loads(payload["reply_markup"]), markup)

    def test_returns_response(self) -> None:
        resp = api.send_message("TOK", 1, "x")
        self.assertIs(resp, self.spy.resp)


class AnswerCallbackQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = api.requests

    def tearDown(self) -> None:
        api.requests = self._orig

    def test_posts_to_answer_callback_query(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, {"ok": True}))
        api.requests = spy
        ok = api.answer_callback_query("TOK", "CBID", text="👍 registrato")
        self.assertTrue(ok)
        call = spy.calls[0]
        self.assertTrue(call["url"].endswith("/answerCallbackQuery"))
        self.assertEqual(call["json"]["callback_query_id"], "CBID")
        self.assertEqual(call["json"]["text"], "👍 registrato")

    def test_text_omitted_when_none(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, {"ok": True}))
        api.requests = spy
        api.answer_callback_query("TOK", "CBID")
        self.assertNotIn("text", spy.calls[0]["json"])

    def test_non_200_returns_false(self) -> None:
        spy = _RequestsSpy(_FakeResp(403, {"ok": False}))
        api.requests = spy
        self.assertFalse(api.answer_callback_query("TOK", "CBID"))

    def test_network_error_swallowed_returns_false(self) -> None:
        spy = _RequestsSpy(resp=Exception("boom"))
        # Make the spy's exception type match what the function catches.
        spy.RequestException = Exception
        api.requests = spy
        # requests.RequestException is what the function catches; alias it.
        api.requests.RequestException = Exception
        self.assertFalse(api.answer_callback_query("TOK", "CBID"))

    def test_bad_json_returns_false_not_raises(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, raise_on_json=True))
        api.requests = spy
        self.assertFalse(api.answer_callback_query("TOK", "CBID"))


class EditMessageReplyMarkupTest(unittest.TestCase):
    """Refinement 1: editMessageReplyMarkup swaps a message's inline keyboard,
    JSON-serialising reply_markup, and is fully defensive (never raises)."""

    def setUp(self) -> None:
        self._orig = api.requests

    def tearDown(self) -> None:
        api.requests = self._orig

    def test_posts_with_json_serialised_markup(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, {"ok": True}))
        api.requests = spy
        markup = {"inline_keyboard": [[{"text": "✅ 👍", "callback_data": "fb:u:k"},
                                       {"text": "👎", "callback_data": "fb:d:k"}]]}
        ok = api.edit_message_reply_markup("TOK", 42, 99, markup)
        self.assertTrue(ok)
        call = spy.calls[0]
        self.assertTrue(call["url"].endswith("/editMessageReplyMarkup"))
        self.assertEqual(call["json"]["chat_id"], 42)
        self.assertEqual(call["json"]["message_id"], 99)
        # reply_markup must be a JSON *string* in the form field.
        self.assertIsInstance(call["json"]["reply_markup"], str)
        self.assertEqual(json.loads(call["json"]["reply_markup"]), markup)

    def test_none_markup_strips_keyboard(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, {"ok": True}))
        api.requests = spy
        api.edit_message_reply_markup("TOK", 42, 99, None)
        self.assertNotIn("reply_markup", spy.calls[0]["json"])

    def test_non_200_returns_false(self) -> None:
        spy = _RequestsSpy(_FakeResp(400, {"ok": False}))
        api.requests = spy
        self.assertFalse(api.edit_message_reply_markup("TOK", 1, 2, None))

    def test_network_error_swallowed_returns_false(self) -> None:
        spy = _RequestsSpy(resp=Exception("boom"))
        spy.RequestException = Exception
        api.requests = spy
        api.requests.RequestException = Exception
        self.assertFalse(api.edit_message_reply_markup("TOK", 1, 2, None))

    def test_bad_json_returns_false_not_raises(self) -> None:
        spy = _RequestsSpy(_FakeResp(200, raise_on_json=True))
        api.requests = spy
        self.assertFalse(api.edit_message_reply_markup("TOK", 1, 2, None))


if __name__ == "__main__":
    unittest.main()
