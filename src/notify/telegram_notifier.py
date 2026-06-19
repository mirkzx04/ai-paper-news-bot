"""Telegram notifier — sends each item as a message via the Bot API.

Stateless `sendMessage` calls (just HTTP POST), which is exactly what the
GitHub Actions deployment needs — no webhook, no long-running process. One
message per item keeps it forward-compatible with the Phase 3 feedback loop,
where each message gets inline 👍/👎 buttons.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time

import requests

from src.notify.base import Notifier, ScoredItem
from src.store.sent_items_store import token_for_key

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

# Short summary budget: keep notifications skimmable.
_SUMMARY_MAX_CHARS = 300
_SUMMARY_MAX_SENTENCES = 2

# Inline 👍/👎 feedback buttons. callback_data is "fb:<u|d>:<token>", where the
# token is the paper's canonical key when it fits Telegram's 64-byte budget, or a
# short hash otherwise (see sent_items_store.token_for_key). The poller parses
# this prefix back into a vote signal + token.
_FEEDBACK_UP_PREFIX = "fb:u:"
_FEEDBACK_DOWN_PREFIX = "fb:d:"


def _short_summary(text: str) -> str:
    """Shorten an abstract to ~2 sentences / ~300 chars on a clean boundary.

    Only shortens the existing text — never rewrites it. Appends "…" when the
    text was actually truncated.
    """
    text = " ".join((text or "").split())  # collapse whitespace/newlines
    if not text:
        return ""

    # First, try to cut on a sentence boundary (keep up to N sentences).
    sentences = re.split(r"(?<=[.!?])\s+", text)
    candidate = " ".join(sentences[:_SUMMARY_MAX_SENTENCES]).strip()

    truncated = candidate != text

    # Then enforce the hard character budget, cutting on a word boundary.
    if len(candidate) > _SUMMARY_MAX_CHARS:
        clipped = candidate[:_SUMMARY_MAX_CHARS]
        # Backtrack to the last whitespace so we don't cut a word in half.
        space = clipped.rfind(" ")
        if space > 0:
            clipped = clipped[:space]
        candidate = clipped.rstrip(" ,;:.!?")
        truncated = True

    return f"{candidate}…" if truncated else candidate


class TelegramNotifier(Notifier):
    def __init__(
        self,
        token: str,
        chat_id: str,
        parse_mode: str = "HTML",
        throttle: float = 0.5,   # stay under Telegram's ~1 msg/s per-chat limit
        timeout: int = 20,
        field_classifier=None,   # duck-typed: .classify(item) -> list[str]
        sent_items=None,         # optional SentItemsStore: enables 👍/👎 buttons
        preference_dataset=None,  # optional PreferenceDataset: enables impression logging
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.throttle = throttle
        self.timeout = timeout
        self.field_classifier = field_classifier
        # When None, no inline keyboard is attached and nothing is recorded — the
        # notifier behaves byte-for-byte as before. When a SentItemsStore is
        # injected, each message gets 👍/👎 buttons and the paper is recorded so
        # a later callback_query can be turned into a vote.
        self.sent_items = sent_items
        # When None, no impression is logged — the notifier behaves exactly as
        # before. When a PreferenceDataset is injected, every paper actually sent
        # is logged as an ``impression`` event (the exposure half of the feedback
        # signal: shown-but-not-necessarily-voted). CRITICAL: impressions are
        # eval/analysis-only WEAK negatives; they MUST NOT feed the scoring loop.
        # The embedding feedback vectors read ``events(types=["vote"])`` only, so
        # impressions never influence ranking (see src/embedding/feedback_vectors).
        self.preference_dataset = preference_dataset

    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        if not scored:
            return
        ordered = sorted(scored, key=lambda x: x.result.total, reverse=True)
        for s in ordered:
            markup = self._feedback_markup(s) if self.sent_items is not None else None
            resp = self._send(self._format(s, kind), reply_markup=markup)
            # Best-effort: record the shown paper so the (much later) 👍/👎 tap can
            # recover its text/score/breakdown. A recording failure must not stop
            # the send loop, so it's fully guarded.
            if self.sent_items is not None:
                self._record_sent(s, resp)
            # Log the exposure (impression) ONLY when the message actually went out
            # (resp is the Telegram result dict on success, None on failure): an
            # impression must mean the user really saw the paper. Best-effort.
            if self.preference_dataset is not None and resp is not None:
                self._record_impression(s, kind)
            time.sleep(self.throttle)

    def _feedback_markup(self, s: ScoredItem) -> dict:
        """Single-row inline keyboard: 👍 / 👎, each carrying its callback token."""
        token = token_for_key(s.item.canonical_key)
        return {"inline_keyboard": [[
            {"text": "👍", "callback_data": _FEEDBACK_UP_PREFIX + token},
            {"text": "👎", "callback_data": _FEEDBACK_DOWN_PREFIX + token},
        ]]}

    def _record_sent(self, s: ScoredItem, resp) -> None:
        """Persist the paper behind a just-sent message into the sent_items store.

        `text` (title + abstract) is the essential field — the scorer re-embeds it
        when the vote lands. `score`/`breakdown` come straight from the ScoredItem
        the notifier already holds, so the vote can be logged with the exact
        ranker context it was shown with. NEVER raises (the store is defensive,
        and this is wrapped too).
        """
        try:
            self.sent_items.record(
                s.item.canonical_key,
                text=s.item.text(),
                score=s.result.total,
                breakdown=dict(s.result.breakdown),
            )
        except Exception as exc:  # noqa: BLE001 — feedback bookkeeping must not break sending
            logger.warning("failed to record sent item %s: %s", s.item.canonical_key, exc)

    def _record_impression(self, s: ScoredItem, kind: str) -> None:
        """Append an ``impression`` event for a paper that was just shown.

        Captures the exposure half of the feedback signal — what the user was
        presented with, regardless of whether they 👍/👎 it — with the exact
        ranker context (``score``/``breakdown``) and the ``route`` (``kind``,
        i.e. "alert"/"digest"). This is WEAK, eval/analysis-only data: it is
        deliberately NOT read by the embedding feedback loop (which consumes only
        ``vote`` events), so a shown-but-unvoted paper can never penalise similar
        papers and collapse recommendation diversity.

        NEVER raises: an impression-logging failure must not stop the send loop
        (PreferenceDataset.log is itself defensive, and this is wrapped too).
        Schema (matches PreferenceDataset's documented ``impression`` event):
            {"type": "impression", "canonical_key": str, "score": float|None,
             "breakdown": dict|None, "route": "alert"|"digest"}
        """
        try:
            self.preference_dataset.log({
                "type": "impression",
                "canonical_key": s.item.canonical_key,
                "score": s.result.total,
                "breakdown": dict(s.result.breakdown),
                "route": kind,
            })
        except Exception as exc:  # noqa: BLE001 — impression logging must not break sending
            logger.warning("failed to log impression for %s: %s", s.item.canonical_key, exc)

    def _send(self, text: str, reply_markup: dict | None = None):
        """POST one message. Returns the parsed Telegram ``result`` dict on success
        (so the caller can read ``message_id``), or ``None`` on any failure.

        `reply_markup` is optional and JSON-serialised as the Bot API requires;
        without it the payload is identical to before.
        """
        url = _API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("telegram send error: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("telegram send failed %s: %s", resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json().get("result")
        except ValueError:
            return None

    def _format(self, s: ScoredItem, kind: str) -> str:
        item = s.item

        # Authors: cap the visible list, mark overflow with "et al.".
        authors = ", ".join(item.authors[:5])
        if len(item.authors) > 5:
            authors += " et al."

        # Research field(s) from the optional duck-typed classifier.
        fields: list[str] = []
        if self.field_classifier is not None:
            fields = self.field_classifier.classify(item)

        summary = _short_summary(item.summary)

        # Each block is its own paragraph (blank line between paragraphs).
        # Order: Title, Authors, Field, Venue, Date, Summary, Link.
        title_prefix = "🔔 ALERT — " if kind == "alert" else ""
        paragraphs: list[str] = [
            f"📄 <b>Title:</b> {title_prefix}{html.escape(item.title)}",
        ]
        if authors:
            paragraphs.append(f"👤 <b>Authors:</b> {html.escape(authors)}")
        if fields:
            paragraphs.append(f"🏷 <b>Field:</b> {html.escape(', '.join(fields))}")
        if item.venue:
            paragraphs.append(f"🎓 <b>Venue:</b> {html.escape(item.venue)}")
        # Release date — always present (every item has a tz-aware `published`).
        paragraphs.append(f"📅 <b>Date:</b> {item.published:%Y-%m-%d}")
        if summary:
            paragraphs.append(f"📝 <b>Summary:</b> {html.escape(summary)}")
        paragraphs.append(f"🔗 {html.escape(item.url)}")

        return "\n\n".join(paragraphs)
