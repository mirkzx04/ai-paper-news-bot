"""`SentItemsStore` — short-lived record of papers shown with 👍/👎 buttons.

The feedback loop is asynchronous and the deployment is stateless: a paper is
sent in one cron run, but the user's 👍/👎 tap arrives as a ``callback_query``
in a *later* run, hours afterwards. The ``callback_data`` Telegram echoes back is
capped at 64 bytes, so it can't carry the paper's text (which the scorer needs to
re-embed the voted paper). This store bridges that gap: at send time we record
``token -> {canonical_key, text, score, breakdown, sent_at}``; at vote time the
callback handler looks the token up and recovers everything it needs.

Design choices (kept faithful to the existing `SqliteStore` pattern):

* **Same database file** as `SqliteStore` (``data/bot.db``, the gist-synced
  ``data/`` tree) but a **separate table** ``sent_items`` and a **separate
  class**. The `Store` ABC (seen-ids + meta) and this transient, TTL'd feedback
  buffer are different concerns; keeping them in distinct classes preserves SRP
  while still co-locating the bytes in one persisted db. The class opens its own
  connection to that same file — exactly like `SqliteStore` does.

* **`token` is the primary key** and is what rides inside ``callback_data``
  (after the ``fb:u:`` / ``fb:d:`` prefix). When ``"fb:u:" + canonical_key``
  fits in Telegram's 64-byte budget the token *is* the canonical key (no
  indirection); when it doesn't, the caller passes a short hash as the token.
  Either way the callback handler resolves purely by token, and the row also
  carries ``canonical_key`` so the written ``vote`` event always has the real
  paper id.

* **Bounded growth.** Every write prunes the table: rows older than
  ``ttl_days`` are deleted, and the table is capped to the newest
  ``max_records`` rows (ring-buffer). The vote can arrive at most a couple of
  runs later, so a ~30-day / ~1000-row window is generous.

Robustness mirrors the rest of the codebase: the path is injectable, breakdown
is stored as JSON text, and reads/writes are defensive — a feedback-store hiccup
must never break message sending or polling. ``record`` and ``prune`` never
raise toward the caller; ``get`` returns ``None`` on any failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Telegram caps callback_data at 64 bytes. Our prefixes ("fb:u:" / "fb:d:") are
# 5 ASCII bytes, so a canonical_key can be used verbatim only if it fits in the
# remaining budget; otherwise we fall back to a short hash token.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_PREFIX_BYTES = len("fb:u:")  # both prefixes are the same length
_MAX_KEY_BYTES = _CALLBACK_DATA_MAX_BYTES - _CALLBACK_PREFIX_BYTES  # 59
# Length (hex chars) of the fallback hash token. 16 hex chars = 64 bits of a
# SHA-1 digest: collision-safe for the at-most-a-few-thousand live rows here,
# and well within the byte budget (5 + 16 = 21 bytes).
_HASH_TOKEN_LEN = 16


def token_for_key(canonical_key: str) -> str:
    """Return the ``callback_data`` token to use for ``canonical_key``.

    The canonical key itself when ``"fb:u:" + key`` fits in 64 bytes (so the key
    survives round-trip in the button and no lookup indirection is needed); a
    short, stable SHA-1-derived hex token otherwise. Deterministic: the same key
    always maps to the same token, so a re-send reuses the same row.
    """
    if len(canonical_key.encode("utf-8")) <= _MAX_KEY_BYTES:
        return canonical_key
    digest = hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()
    return digest[:_HASH_TOKEN_LEN]


class SentItemsStore:
    def __init__(self, path: str = "data/bot.db", *,
                 ttl_days: int = 30, max_records: int = 1000) -> None:
        self.path = path
        self.ttl_days = ttl_days
        self.max_records = max_records
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_items ("
            "  token TEXT PRIMARY KEY,"
            "  canonical_key TEXT NOT NULL,"
            "  text TEXT,"
            "  score REAL,"
            "  breakdown TEXT,"          # JSON-encoded per-scorer dict, or NULL
            "  sent_at TEXT NOT NULL"     # UTC ISO-8601
            ")"
        )
        self.conn.commit()

    # ---- writing -----------------------------------------------------------
    def record(self, canonical_key: str, text: str | None = None, *,
               score: float | None = None, breakdown: dict | None = None,
               token: str | None = None, sent_at: datetime | None = None) -> str:
        """Persist the paper behind a sent message and return its token.

        `token` defaults to :func:`token_for_key(canonical_key)`; pass it
        explicitly only if the notifier already computed it for the button. A
        re-send of the same paper overwrites the prior row (same token PK). Every
        call prunes the table (TTL + ring-buffer). NEVER raises: on failure it
        logs and still returns the token so the caller's button stays valid.
        """
        tok = token if token is not None else token_for_key(canonical_key)
        when = (sent_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            breakdown_json = (
                json.dumps(breakdown, ensure_ascii=False) if breakdown is not None else None
            )
            self.conn.execute(
                "INSERT INTO sent_items (token, canonical_key, text, score, breakdown, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET "
                "  canonical_key = excluded.canonical_key, text = excluded.text, "
                "  score = excluded.score, breakdown = excluded.breakdown, "
                "  sent_at = excluded.sent_at",
                (tok, canonical_key, text, score, breakdown_json, when.isoformat()),
            )
            self.conn.commit()
            self._prune(now=when)
        except Exception as exc:  # noqa: BLE001 — feedback store must never break sending
            logger.warning("sent_items record failed for %s: %s", canonical_key, exc)
        return tok

    def _prune(self, *, now: datetime) -> None:
        """Drop expired (older than ttl_days) and overflow (> max_records) rows.

        Defensive: pruning is best-effort housekeeping, never fatal.
        """
        try:
            cutoff = (now - timedelta(days=self.ttl_days)).isoformat()
            self.conn.execute("DELETE FROM sent_items WHERE sent_at < ?", (cutoff,))
            # Ring-buffer: keep only the newest `max_records` rows. ROWID is
            # monotonic with insertion order, so it breaks ties when sent_at is
            # equal and avoids a correlated subquery per row.
            self.conn.execute(
                "DELETE FROM sent_items WHERE token IN ("
                "  SELECT token FROM sent_items "
                "  ORDER BY sent_at DESC, rowid DESC "
                "  LIMIT -1 OFFSET ?"
                ")",
                (self.max_records,),
            )
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sent_items prune failed: %s", exc)

    # ---- reading -----------------------------------------------------------
    def get(self, token: str) -> dict | None:
        """Look up a sent paper by its callback token.

        Returns ``{canonical_key, text, score, breakdown, sent_at}`` (breakdown
        decoded back to a dict, or ``None``) or ``None`` if the token is unknown
        (e.g. the row was pruned because the vote arrived too late). NEVER raises.
        """
        try:
            cur = self.conn.execute(
                "SELECT canonical_key, text, score, breakdown, sent_at "
                "FROM sent_items WHERE token = ?",
                (token,),
            )
            row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sent_items get failed for %s: %s", token, exc)
            return None
        if row is None:
            return None
        breakdown = None
        if row[3] is not None:
            try:
                breakdown = json.loads(row[3])
            except (ValueError, TypeError):
                breakdown = None
        return {
            "canonical_key": row[0],
            "text": row[1],
            "score": row[2],
            "breakdown": breakdown,
            "sent_at": row[4],
        }

    def count(self) -> int:
        """Number of rows currently stored (0 on any error). NEVER raises."""
        try:
            cur = self.conn.execute("SELECT COUNT(*) FROM sent_items")
            return int(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("sent_items count failed: %s", exc)
            return 0

    def close(self) -> None:
        self.conn.close()
