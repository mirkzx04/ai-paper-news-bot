"""Tests for `SentItemsStore` and the `token_for_key` callback-data helper.

Covers: write/read round-trip (incl. breakdown JSON), the 64-byte callback_data
budget and hash-token fallback, TTL expiry, ring-buffer capping, defensive reads
of unknown tokens, and that the store lives in the same db file as `SqliteStore`
without clashing. Pure stdlib `unittest` (no pytest dep).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store.sent_items_store import (  # noqa: E402
    _CALLBACK_DATA_MAX_BYTES,
    SentItemsStore,
    token_for_key,
)


class TokenForKeyTest(unittest.TestCase):
    def test_short_key_used_verbatim(self) -> None:
        # An arXiv canonical key is short; the token IS the key (no indirection).
        key = "arxiv:2401.12345"
        self.assertEqual(token_for_key(key), key)

    def test_callback_data_fits_budget_for_short_key(self) -> None:
        key = "arxiv:2401.12345"
        self.assertLessEqual(len(("fb:u:" + token_for_key(key)).encode("utf-8")),
                             _CALLBACK_DATA_MAX_BYTES)

    def test_long_key_falls_back_to_hash_within_budget(self) -> None:
        # A Bluesky AT-URI style key easily exceeds 59 bytes.
        key = "bluesky:at://did:plc:" + "x" * 90
        token = token_for_key(key)
        self.assertNotEqual(token, key)
        # The button payload must stay within Telegram's 64-byte cap.
        self.assertLessEqual(len(("fb:u:" + token).encode("utf-8")),
                             _CALLBACK_DATA_MAX_BYTES)

    def test_hash_token_is_deterministic(self) -> None:
        key = "hf:" + "y" * 120
        self.assertEqual(token_for_key(key), token_for_key(key))

    def test_distinct_long_keys_get_distinct_tokens(self) -> None:
        a = token_for_key("bluesky:" + "a" * 100)
        b = token_for_key("bluesky:" + "b" * 100)
        self.assertNotEqual(a, b)

    def test_boundary_key_exactly_at_limit_used_verbatim(self) -> None:
        # 59 ASCII bytes after the 5-byte prefix == exactly 64 bytes: still verbatim.
        key = "k" * 59
        self.assertEqual(token_for_key(key), key)
        self.assertEqual(len(("fb:u:" + key).encode("utf-8")), _CALLBACK_DATA_MAX_BYTES)

    def test_one_over_limit_key_hashed(self) -> None:
        key = "k" * 60  # 5 + 60 = 65 > 64
        self.assertNotEqual(token_for_key(key), key)


class SentItemsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bot.db")
        self.store = SentItemsStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_record_and_get_round_trip(self) -> None:
        self.store.record("arxiv:2401.00001", text="Title\n\nAbstract",
                          score=0.7, breakdown={"keyword": 0.3, "embedding": 0.6})
        rec = self.store.get("arxiv:2401.00001")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["canonical_key"], "arxiv:2401.00001")
        self.assertEqual(rec["text"], "Title\n\nAbstract")
        self.assertAlmostEqual(rec["score"], 0.7)
        self.assertEqual(rec["breakdown"], {"keyword": 0.3, "embedding": 0.6})
        # sent_at is a UTC ISO-8601 string.
        self.assertIsNotNone(datetime.fromisoformat(rec["sent_at"]).tzinfo)

    def test_record_returns_token(self) -> None:
        tok = self.store.record("arxiv:2401.00002", text="x")
        self.assertEqual(tok, "arxiv:2401.00002")  # short key => token is the key

    def test_long_key_stored_under_hash_token_and_resolvable(self) -> None:
        key = "bluesky:at://did:plc:" + "z" * 90
        tok = self.store.record(key, text="Long paper", score=0.9, breakdown={"a": 1.0})
        self.assertNotEqual(tok, key)
        rec = self.store.get(tok)  # callback resolves purely by token
        self.assertEqual(rec["canonical_key"], key)
        self.assertEqual(rec["text"], "Long paper")

    def test_get_unknown_token_returns_none(self) -> None:
        self.assertIsNone(self.store.get("arxiv:nope"))

    def test_none_text_and_breakdown_round_trip(self) -> None:
        self.store.record("arxiv:2401.00003")  # text/score/breakdown default None
        rec = self.store.get("arxiv:2401.00003")
        self.assertIsNone(rec["text"])
        self.assertIsNone(rec["score"])
        self.assertIsNone(rec["breakdown"])

    def test_resend_overwrites_same_row(self) -> None:
        self.store.record("arxiv:k", text="first", score=0.1)
        self.store.record("arxiv:k", text="second", score=0.9)
        rec = self.store.get("arxiv:k")
        self.assertEqual(rec["text"], "second")
        self.assertAlmostEqual(rec["score"], 0.9)
        self.assertEqual(self.store.count(), 1)  # not duplicated

    def test_ttl_prunes_old_rows_on_write(self) -> None:
        store = SentItemsStore(os.path.join(self.tmp.name, "ttl.db"), ttl_days=30)
        old = datetime.now(timezone.utc) - timedelta(days=40)
        store.record("arxiv:old", text="old", sent_at=old)
        # Any subsequent write triggers a prune; the 40-day-old row must vanish.
        store.record("arxiv:fresh", text="fresh")
        self.assertIsNone(store.get("arxiv:old"))
        self.assertIsNotNone(store.get("arxiv:fresh"))
        store.close()

    def test_ttl_keeps_rows_within_window(self) -> None:
        store = SentItemsStore(os.path.join(self.tmp.name, "ttl2.db"), ttl_days=30)
        recent = datetime.now(timezone.utc) - timedelta(days=5)
        store.record("arxiv:recent", text="r", sent_at=recent)
        store.record("arxiv:newer", text="n")
        self.assertIsNotNone(store.get("arxiv:recent"))
        store.close()

    def test_ring_buffer_caps_to_max_records(self) -> None:
        store = SentItemsStore(os.path.join(self.tmp.name, "ring.db"), max_records=3)
        base = datetime.now(timezone.utc)
        for i in range(6):
            store.record(f"arxiv:r{i}", text=f"t{i}", sent_at=base + timedelta(seconds=i))
        self.assertEqual(store.count(), 3)
        # Only the three newest survive.
        self.assertIsNone(store.get("arxiv:r0"))
        self.assertIsNone(store.get("arxiv:r2"))
        self.assertIsNotNone(store.get("arxiv:r3"))
        self.assertIsNotNone(store.get("arxiv:r5"))
        store.close()

    def test_shares_db_file_with_sqlite_store_without_clash(self) -> None:
        # The feedback table coexists with SqliteStore's seen/meta in one db file.
        from src.store.sqlite_store import SqliteStore
        sq = SqliteStore(self.path)
        sq.set_meta("telegram_offset", "42")
        self.store.record("arxiv:coexist", text="ok")
        self.assertEqual(sq.get_meta("telegram_offset"), "42")
        self.assertIsNotNone(self.store.get("arxiv:coexist"))
        sq.close()


if __name__ == "__main__":
    unittest.main()
