"""Tests for the 👍/👎 feedback loop: notifier wiring + poller callback handling.

Two halves:

* **Notifier** — with a `SentItemsStore` injected, each message carries a
  single-row 👍/👎 inline keyboard whose callback_data is "fb:<u|d>:<token>",
  and the shown paper (text/score/breakdown) is recorded so a later vote can
  recover it. With NO store injected, behaviour is unchanged (no keyboard, no
  record).
* **Poller** — a callback_query is parsed, resolved against `sent_items`, turned
  into a `vote` event with the documented schema, deduped (idempotent re-tap,
  flip on the other emoji), and ALWAYS acknowledged. With `preference_dataset`
  unset the feature is off: callbacks are ignored (no vote) but still acked, and
  normal messages keep working.

We monkeypatch the telegram_api names the poller imported (get_updates,
send_message, answer_callback_query) and `requests` inside the notifier, so
nothing hits the network. Stdlib `unittest` only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import telegram_poller as tp_mod  # noqa: E402
from src.domain.item import Item  # noqa: E402
from src.notify import telegram_notifier as tn_mod  # noqa: E402
from src.notify.base import ScoredItem  # noqa: E402
from src.notify.telegram_notifier import TelegramNotifier  # noqa: E402
from src.scoring.base import ScoreResult  # noqa: E402
from src.store.preference_dataset import PreferenceDataset  # noqa: E402
from src.store.sent_items_store import SentItemsStore, token_for_key  # noqa: E402
from src.telegram_poller import TelegramPoller, _feedback_markup  # noqa: E402


# ----------------------------------------------------------------- helpers --
def _item(external_id="2401.12345", source="arxiv", title="A Title",
          summary="An abstract.", url="http://example/x") -> Item:
    return Item(source=source, external_id=external_id, title=title, summary=summary,
                url=url, published=datetime.now(timezone.utc))


def _scored(item=None, total=0.8, breakdown=None) -> ScoredItem:
    return ScoredItem(item or _item(),
                      ScoreResult(total=total, breakdown=breakdown or {"keyword": 0.5}))


def _cb_update(update_id, data, cq_id, reply_markup=None):
    message = {"message_id": 99, "chat": {"id": 7}}
    if reply_markup is not None:
        # Mirrors what Telegram echoes back on the callback's message; lets the
        # poller detect a no-op keyboard edit (skip an identical editMessageReplyMarkup).
        message["reply_markup"] = reply_markup
    return {"update_id": update_id,
            "callback_query": {"id": cq_id, "data": data, "from": {"id": 7},
                               "message": message}}


def _msg_update(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"message_id": 1000 + update_id, "chat": {"id": chat_id}, "text": text}}


class _Store:
    def __init__(self) -> None:
        self._meta: dict = {}

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


class _Dispatcher:
    def __init__(self, reply=None) -> None:
        self.reply = reply
        self.seen: list[str] = []

    def dispatch(self, text):
        self.seen.append(text)
        return self.reply


# =============================================================== notifier ==
class _RequestsSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.RequestException = Exception

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return _Resp()


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True, "result": {"message_id": 555}}


class NotifierFeedbackWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = tn_mod.requests
        self.spy = _RequestsSpy()
        tn_mod.requests = self.spy
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SentItemsStore(os.path.join(self.tmp.name, "bot.db"))

    def tearDown(self) -> None:
        tn_mod.requests = self._orig
        self.store.close()
        self.tmp.cleanup()

    def test_no_store_no_keyboard_no_record(self) -> None:
        # Backward-compat: without sent_items, the payload has no reply_markup
        # and nothing is recorded.
        notifier = TelegramNotifier("TOK", "7", throttle=0)
        notifier.notify([_scored()], kind="digest")
        payload = self.spy.calls[0]["json"]
        self.assertNotIn("reply_markup", payload)
        self.assertEqual(self.store.count(), 0)

    def test_store_attaches_keyboard_and_records(self) -> None:
        notifier = TelegramNotifier("TOK", "7", throttle=0, sent_items=self.store)
        item = _item(external_id="2401.99999")
        s = _scored(item=item, total=0.83, breakdown={"keyword": 0.4, "embedding": 0.6})
        notifier.notify([s], kind="alert")

        payload = self.spy.calls[0]["json"]
        self.assertIn("reply_markup", payload)
        import json as _json
        markup = _json.loads(payload["reply_markup"])
        row = markup["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["👍", "👎"])
        token = token_for_key(item.canonical_key)
        self.assertEqual(row[0]["callback_data"], "fb:u:" + token)
        self.assertEqual(row[1]["callback_data"], "fb:d:" + token)

        # The paper was recorded with the essential text + the ranker context.
        rec = self.store.get(token)
        self.assertEqual(rec["canonical_key"], item.canonical_key)
        self.assertEqual(rec["text"], item.text())
        self.assertAlmostEqual(rec["score"], 0.83)
        self.assertEqual(rec["breakdown"], {"keyword": 0.4, "embedding": 0.6})

    def test_record_failure_does_not_break_send(self) -> None:
        # A store that explodes on record must not stop the message going out.
        class _BoomStore:
            def record(self, *a, **k):
                raise RuntimeError("disk full")

        notifier = TelegramNotifier("TOK", "7", throttle=0, sent_items=_BoomStore())
        notifier.notify([_scored()], kind="digest")  # must not raise
        self.assertEqual(len(self.spy.calls), 1)  # message still sent


class _FailingResp:
    """A 200 with no `result` -> TelegramNotifier._send returns None (send failed)."""

    status_code = 200
    text = ""

    def json(self):
        return {"ok": True}  # no "result" key


class _FailingRequestsSpy(_RequestsSpy):
    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return _FailingResp()


class NotifierImpressionTest(unittest.TestCase):
    """Refinement 3: with a PreferenceDataset injected, each SUCCESSFULLY sent
    paper logs one `impression` event (route = kind); without it, nothing is
    logged (backward-compat). A send failure logs no impression, and a logging
    failure never breaks the send loop."""

    def setUp(self) -> None:
        self._orig = tn_mod.requests
        self.spy = _RequestsSpy()
        tn_mod.requests = self.spy
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = PreferenceDataset(os.path.join(self.tmp.name, "prefs.jsonl"))

    def tearDown(self) -> None:
        tn_mod.requests = self._orig
        self.tmp.cleanup()

    def test_no_dataset_logs_no_impression(self) -> None:
        # Backward-compat: without preference_dataset, no impression is written.
        notifier = TelegramNotifier("TOK", "7", throttle=0)
        notifier.notify([_scored()], kind="digest")
        self.assertEqual(self.ds.events(types=["impression"]), [])

    def test_logs_impression_with_full_schema_and_route(self) -> None:
        notifier = TelegramNotifier("TOK", "7", throttle=0, preference_dataset=self.ds)
        item = _item(external_id="2401.40001")
        s = _scored(item=item, total=0.66, breakdown={"keyword": 0.3, "embedding": 0.7})
        notifier.notify([s], kind="alert")
        imps = self.ds.events(types=["impression"])
        self.assertEqual(len(imps), 1)
        imp = imps[0]
        self.assertEqual(imp["type"], "impression")
        self.assertEqual(imp["canonical_key"], item.canonical_key)
        self.assertAlmostEqual(imp["score"], 0.66)
        self.assertEqual(imp["breakdown"], {"keyword": 0.3, "embedding": 0.7})
        self.assertEqual(imp["route"], "alert")  # route == kind

    def test_one_impression_per_sent_paper(self) -> None:
        notifier = TelegramNotifier("TOK", "7", throttle=0, preference_dataset=self.ds)
        batch = [_scored(item=_item(external_id=f"2401.4100{i}"), total=0.5) for i in range(3)]
        notifier.notify(batch, kind="digest")
        self.assertEqual(len(self.ds.events(types=["impression"])), 3)

    def test_failed_send_logs_no_impression(self) -> None:
        # An impression must mean the paper was really shown: a failed send (no
        # `result` in the response) writes NO impression.
        tn_mod.requests = _FailingRequestsSpy()
        notifier = TelegramNotifier("TOK", "7", throttle=0, preference_dataset=self.ds)
        notifier.notify([_scored()], kind="digest")
        self.assertEqual(self.ds.events(types=["impression"]), [])

    def test_impression_logging_failure_does_not_break_send(self) -> None:
        class _BoomDataset:
            def log(self, event):
                raise RuntimeError("disk full")

        notifier = TelegramNotifier("TOK", "7", throttle=0, preference_dataset=_BoomDataset())
        notifier.notify([_scored()], kind="digest")  # must not raise
        self.assertEqual(len(self.spy.calls), 1)  # message still sent

    def test_impressions_and_votes_coexist_in_same_log(self) -> None:
        # Sanity: impressions and votes are independent event types in one file.
        notifier = TelegramNotifier("TOK", "7", throttle=0, preference_dataset=self.ds)
        notifier.notify([_scored(item=_item(external_id="2401.42001"))], kind="digest")
        self.ds.log({"type": "vote", "signal": "up", "canonical_key": "arxiv:2401.42001"})
        self.assertEqual(len(self.ds.events(types=["impression"])), 1)
        self.assertEqual(len(self.ds.events(types=["vote"])), 1)


# ================================================================= poller ==
class PollerFeedbackBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = PreferenceDataset(os.path.join(self.tmp.name, "prefs.jsonl"))
        self.sent_items = SentItemsStore(os.path.join(self.tmp.name, "bot.db"))

        self.sent: list[dict] = []
        self.acks: list[tuple] = []
        self.edits: list[dict] = []  # captured editMessageReplyMarkup calls
        self._updates: list[dict] = []

        self._orig_get = tp_mod.get_updates
        self._orig_send = tp_mod.send_message
        self._orig_ack = tp_mod.answer_callback_query
        self._orig_edit = tp_mod.edit_message_reply_markup

        tp_mod.get_updates = lambda *a, **k: {"ok": True, "result": self._updates}

        def fake_send(token, chat_id, text, parse_mode=None, timeout=20):
            self.sent.append({"chat_id": chat_id, "text": text})
            return None

        def fake_ack(token, cq_id, text=None, timeout=10):
            self.acks.append((cq_id, text))
            return True

        def fake_edit(token, chat_id, message_id, reply_markup, timeout=10):
            self.edits.append({"chat_id": chat_id, "message_id": message_id,
                               "reply_markup": reply_markup})
            return True

        tp_mod.send_message = fake_send
        tp_mod.answer_callback_query = fake_ack
        tp_mod.edit_message_reply_markup = fake_edit

    def tearDown(self) -> None:
        tp_mod.get_updates = self._orig_get
        tp_mod.send_message = self._orig_send
        tp_mod.answer_callback_query = self._orig_ack
        tp_mod.edit_message_reply_markup = self._orig_edit
        self.sent_items.close()
        self.tmp.cleanup()

    def _poller(self, *, with_feedback=True, dispatcher=None):
        return TelegramPoller(
            "TOK", dispatcher or _Dispatcher(), _Store(),
            preference_dataset=self.ds if with_feedback else None,
            sent_items=self.sent_items if with_feedback else None,
        )


class CallbackDataParsingTest(unittest.TestCase):
    def test_valid_up_and_down(self) -> None:
        self.assertEqual(TelegramPoller._parse_feedback_data("fb:u:arxiv:2401.1"),
                         ("up", "arxiv:2401.1"))
        self.assertEqual(TelegramPoller._parse_feedback_data("fb:d:hashtok"),
                         ("down", "hashtok"))

    def test_token_may_contain_colons(self) -> None:
        # Canonical keys (arxiv:2401.1) carry a colon; only the first two split.
        self.assertEqual(TelegramPoller._parse_feedback_data("fb:u:s2:abc:def"),
                         ("up", "s2:abc:def"))

    def test_rejects_malformed(self) -> None:
        for bad in ["xx:u:k", "fb:z:k", "fb:u:", "fb:u", "fb:", "", "random", None, 123, b"fb:u:k"]:
            self.assertIsNone(TelegramPoller._parse_feedback_data(bad), bad)


class PollerVoteWritingTest(PollerFeedbackBase):
    def test_up_vote_writes_full_schema(self) -> None:
        item = _item(external_id="2401.77777")
        key = item.canonical_key
        self.sent_items.record(key, text=item.text(), score=0.71,
                               breakdown={"keyword": 0.2, "author": 1.0})
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + token_for_key(key), "CB1")]
        poller.poll_once()

        votes = self.ds.events(types=["vote"])
        self.assertEqual(len(votes), 1)
        v = votes[0]
        # Exactly the documented vote schema (+ auto ts/type from PreferenceDataset).
        self.assertEqual(v["type"], "vote")
        self.assertEqual(v["signal"], "up")
        self.assertEqual(v["canonical_key"], key)
        self.assertEqual(v["text"], item.text())
        self.assertAlmostEqual(v["score"], 0.71)
        self.assertEqual(v["breakdown"], {"keyword": 0.2, "author": 1.0})
        # The spinner was stopped with the right toast.
        self.assertEqual(self.acks, [("CB1", "👍 registrato")])

    def test_down_vote_toast(self) -> None:
        key = "arxiv:2401.00010"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:d:" + key, "CB")]
        poller.poll_once()
        self.assertEqual(self.ds.events(types=["vote"])[0]["signal"], "down")
        self.assertEqual(self.acks[-1], ("CB", "👎 registrato"))

    def test_unknown_token_still_logs_signal_with_nones(self) -> None:
        # Vote arrives after the row was pruned: the preference signal is NOT lost.
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:arxiv:gone.99999", "CB")]
        poller.poll_once()
        v = self.ds.events(types=["vote"])[0]
        self.assertEqual(v["canonical_key"], "arxiv:gone.99999")
        self.assertEqual(v["signal"], "up")
        self.assertIsNone(v["text"])
        self.assertIsNone(v["score"])
        self.assertIsNone(v["breakdown"])
        self.assertEqual(self.acks[-1], ("CB", "👍 registrato"))

    def test_long_key_resolved_via_hash_token(self) -> None:
        key = "bluesky:at://did:plc:" + "q" * 90
        token = token_for_key(key)
        self.sent_items.record(key, text="long body", score=0.5, token=token)
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + token, "CB")]
        poller.poll_once()
        v = self.ds.events(types=["vote"])[0]
        self.assertEqual(v["canonical_key"], key)  # real key recovered, not the hash
        self.assertEqual(v["text"], "long body")


class PollerDedupTest(PollerFeedbackBase):
    def test_retap_same_emoji_toggles_vote_off(self) -> None:
        # BEHAVIOUR CHANGE (owner-requested, replaces the old idempotent re-tap):
        # re-tapping the emoji of your OWN current vote now WITHDRAWS it. The log
        # is append-only, so the withdrawal is a fresh ``signal:"none"`` event,
        # making the net state "no preference". Both taps are still acked, the
        # second with the toggle-off toast.
        key = "arxiv:2401.55555"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + key, "A")]
        poller.poll_once()
        self._updates = [_cb_update(2, "fb:u:" + key, "B")]
        poller.poll_once()
        votes = self.ds.events(types=["vote"])
        # Two events now (up, then the toggle-off "none") — NOT one.
        self.assertEqual([v["signal"] for v in votes], ["up", "none"])
        # Net state is withdrawn: neither positive nor negative.
        self.assertEqual(poller._current_vote_signal(key), "none")
        self.assertEqual([a[0] for a in self.acks], ["A", "B"])
        self.assertEqual(self.acks[-1][1], "↩️ voto rimosso")  # toggle-off toast

    def test_third_tap_after_toggle_off_revotes(self) -> None:
        # none -> up: re-voting after a withdrawal logs a fresh active vote (the
        # tapped emoji never equals "none", so it is never re-toggled off).
        key = "arxiv:2401.55556"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        for upd_id, cq in ((1, "A"), (2, "B"), (3, "C")):
            self._updates = [_cb_update(upd_id, "fb:u:" + key, cq)]
            poller.poll_once()
        self.assertEqual([v["signal"] for v in self.ds.events(types=["vote"])],
                         ["up", "none", "up"])
        self.assertEqual(poller._current_vote_signal(key), "up")

    def test_flip_to_other_emoji_appends(self) -> None:
        key = "arxiv:2401.44444"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + key, "A")]
        poller.poll_once()
        self._updates = [_cb_update(2, "fb:d:" + key, "B")]
        poller.poll_once()
        votes = self.ds.events(types=["vote"])
        self.assertEqual(len(votes), 2)
        self.assertEqual([v["signal"] for v in votes], ["up", "down"])

    def test_independent_keys_do_not_dedup_against_each_other(self) -> None:
        self.sent_items.record("arxiv:k1", text="t1")
        self.sent_items.record("arxiv:k2", text="t2")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:arxiv:k1", "A")]
        poller.poll_once()
        self._updates = [_cb_update(2, "fb:u:arxiv:k2", "B")]
        poller.poll_once()
        self.assertEqual(len(self.ds.events(types=["vote"])), 2)


class PollerAffordanceTest(PollerFeedbackBase):
    """Refinement 1: after a vote, the inline keyboard is re-rendered to mark the
    chosen option, with chat_id/message_id taken from the callback's message and
    callback_data preserved so the user can keep (re-)voting."""

    def test_up_vote_marks_thumbs_up_and_keeps_callback_data(self) -> None:
        key = "arxiv:2401.30001"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + key, "A")]
        poller.poll_once()
        self.assertEqual(len(self.edits), 1)
        edit = self.edits[0]
        # chat_id / message_id come straight from the callback's message.
        self.assertEqual(edit["chat_id"], 7)
        self.assertEqual(edit["message_id"], 99)
        row = edit["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["✅ 👍", "👎"])  # 👍 marked
        # callback_data is UNCHANGED so a re-vote still works.
        self.assertEqual(row[0]["callback_data"], "fb:u:" + key)
        self.assertEqual(row[1]["callback_data"], "fb:d:" + key)

    def test_down_vote_marks_thumbs_down(self) -> None:
        key = "arxiv:2401.30002"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:d:" + key, "A")]
        poller.poll_once()
        row = self.edits[0]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["👍", "✅ 👎"])

    def test_toggle_off_restores_neutral_keyboard(self) -> None:
        key = "arxiv:2401.30003"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        # Vote up (marks 👍), then re-tap up to withdraw (restores neutral).
        self._updates = [_cb_update(1, "fb:u:" + key, "A")]
        poller.poll_once()
        self._updates = [_cb_update(2, "fb:u:" + key, "B")]
        poller.poll_once()
        row = self.edits[-1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["👍", "👎"])  # both neutral

    def test_skips_edit_when_keyboard_unchanged(self) -> None:
        # If the message already carries the exact target keyboard, no edit is
        # sent (Telegram rejects an identical editMessageReplyMarkup as "not
        # modified"). Here the callback's message already shows 👍 marked, and we
        # vote up again-as-flip... so simulate the already-up keyboard with a
        # fresh paper whose current keyboard equals the post-vote one.
        key = "arxiv:2401.30004"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        already = _feedback_markup(token_for_key(key), "up")
        # Tapping DOWN on a paper that already shows 👍-marked -> target is the
        # 👎-marked keyboard, which DIFFERS, so an edit IS sent. To hit the skip
        # path we instead make the message already equal the target: tap up while
        # the message already shows up-marked AND there is no prior vote, so the
        # new net state is "up" and the target equals `already`.
        self._updates = [_cb_update(1, "fb:u:" + key, "A", reply_markup=already)]
        poller.poll_once()
        self.assertEqual(self.edits, [])  # no-op edit suppressed
        # The vote itself was still recorded and acked.
        self.assertEqual(self.ds.events(types=["vote"])[0]["signal"], "up")
        self.assertEqual(self.acks[-1][0], "A")

    def test_edit_failure_does_not_break_poll_or_lose_vote(self) -> None:
        # A raising edit_message_reply_markup must not crash the poll; the vote is
        # already persisted before the cosmetic edit is attempted.
        def boom_edit(*a, **k):
            raise RuntimeError("message to edit not found")

        tp_mod.edit_message_reply_markup = boom_edit
        key = "arxiv:2401.30005"
        self.sent_items.record(key, text="t")
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:u:" + key, "A")]
        poller.poll_once()  # must not raise
        self.assertEqual(self.ds.events(types=["vote"])[0]["signal"], "up")
        self.assertEqual(self.acks[-1][0], "A")  # spinner still stopped


class PollerMalformedAndBackwardCompatTest(PollerFeedbackBase):
    def test_malformed_callback_no_vote_but_acked(self) -> None:
        poller = self._poller()
        self._updates = [_cb_update(1, "fb:z:bad", "CB")]
        poller.poll_once()
        self.assertEqual(self.ds.events(types=["vote"]), [])
        self.assertEqual(self.acks, [("CB", None)])  # acked, no toast

    def test_feature_off_ignores_callbacks_but_acks(self) -> None:
        # preference_dataset unset => no vote logged, but spinner still stopped.
        poller = self._poller(with_feedback=False)
        self._updates = [_cb_update(1, "fb:u:arxiv:2401.1", "CB")]
        poller.poll_once()
        self.assertEqual(self.ds.events(types=["vote"]), [])
        self.assertEqual(self.acks, [("CB", None)])

    def test_feature_off_normal_messages_still_dispatched(self) -> None:
        disp = _Dispatcher(reply="OK")
        poller = self._poller(with_feedback=False, dispatcher=disp)
        self._updates = [
            _cb_update(1, "fb:u:arxiv:2401.1", "CB"),
            _msg_update(2, 7, "/hello"),
        ]
        replies = poller.poll_once()
        self.assertEqual(disp.seen, ["/hello"])
        self.assertEqual(replies, 1)
        self.assertEqual(self.sent[0]["text"], "OK")

    def test_callback_does_not_advance_into_message_path(self) -> None:
        # A callback update must not be mistaken for a message (no chat/text crash).
        poller = self._poller()
        self.sent_items.record("arxiv:2401.1", text="t")
        self._updates = [_cb_update(1, "fb:u:arxiv:2401.1", "CB")]
        replies = poller.poll_once()
        self.assertEqual(replies, 0)  # no reply message for a callback
        self.assertEqual(len(self.ds.events(types=["vote"])), 1)

    def test_offset_advances_past_callbacks(self) -> None:
        poller = self._poller()
        self.sent_items.record("arxiv:2401.1", text="t")
        self._updates = [_cb_update(10, "fb:u:arxiv:2401.1", "CB")]
        poller.poll_once()
        self.assertEqual(poller.store.get_meta("telegram_offset"), "11")

    def test_handler_error_is_logged_not_fatal(self) -> None:
        # A broken sent_items.get must be caught: no crash, and the spinner is
        # still acked via the handler's own finally.
        class _BoomGet(SentItemsStore):
            def get(self, token):
                raise RuntimeError("db gone")

        boom = _BoomGet(os.path.join(self.tmp.name, "boom.db"))
        poller = TelegramPoller("TOK", _Dispatcher(), _Store(),
                                preference_dataset=self.ds, sent_items=boom)
        self._updates = [_cb_update(1, "fb:u:arxiv:2401.1", "CB")]
        poller.poll_once()  # must not raise
        # The ack still fired from _handle_callback_query's finally.
        self.assertEqual(self.acks[-1][0], "CB")
        boom.close()


if __name__ == "__main__":
    unittest.main()
