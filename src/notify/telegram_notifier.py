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
from src.telegram_api import PERMANENT_SEND_STATUSES, PermanentSendError

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

# 429 (Too Many Requests) handling. Telegram returns a JSON body
# {"parameters": {"retry_after": <seconds>}} telling us how long to back off.
# We honour it (sleep then re-send) up to a few times, but cap the sleep so a
# hostile/buggy value can't stall the run for minutes.
_DEFAULT_MAX_RETRIES = 3          # total send attempts = 1 initial + (max_retries-1) retries
_DEFAULT_RETRY_AFTER_CAP = 60.0   # seconds; hard ceiling on a single backoff
_FALLBACK_RETRY_AFTER = 1.0       # seconds; used when retry_after is absent/unparsable


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
        throttle: float = 1.0,   # stay under Telegram's ~1 msg/s per-chat limit
        timeout: int = 20,
        field_classifier=None,   # duck-typed: .classify(item) -> list[str]
        sent_items=None,         # optional SentItemsStore: enables 👍/👎 buttons
        preference_dataset=None,  # optional PreferenceDataset: enables impression logging
        user_id: str | None = None,  # optional anonymous user id for impression events
        rate_limiter=None,       # optional shared RateLimiter: paces sends across users
        max_retries: int = _DEFAULT_MAX_RETRIES,        # total send attempts on HTTP 429
        retry_after_cap: float = _DEFAULT_RETRY_AFTER_CAP,  # max seconds to honour per 429
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.throttle = throttle
        self.timeout = timeout
        # At least one attempt, even if a caller passes a nonsensical value.
        self.max_retries = max(1, int(max_retries))
        self.retry_after_cap = float(retry_after_cap)
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
        self.user_id = user_id
        # When None, sends are paced by the blunt per-message ``time.sleep(throttle)``
        # exactly as before. When a shared ``RateLimiter`` is injected (one per
        # digest run, passed to every per-user notifier) it enforces BOTH the
        # per-chat (~1 msg/s) AND the global (~30 msg/s) Telegram caps across all
        # users, and the per-message sleep is dropped in its favour.
        self.rate_limiter = rate_limiter

    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        if not scored:
            return
        ordered = sorted(scored, key=lambda x: x.result.total, reverse=True)
        for s in ordered:
            # Pace BEFORE sending: the shared limiter honours the global+per-chat
            # caps; without one we keep the legacy post-send throttle below.
            if self.rate_limiter is not None:
                self.rate_limiter.acquire(self.chat_id)
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
            if self.rate_limiter is None:
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
            event = {
                "type": "impression",
                "canonical_key": s.item.canonical_key,
                "score": s.result.total,
                "breakdown": dict(s.result.breakdown),
                "route": kind,
            }
            if self.user_id is not None:
                event["user_id"] = self.user_id
            self.preference_dataset.log(event)
        except Exception as exc:  # noqa: BLE001 — impression logging must not break sending
            logger.warning("failed to log impression for %s: %s", s.item.canonical_key, exc)

    def _retry_after_seconds(self, resp) -> float:
        """Read Telegram's ``retry_after`` (seconds) from a 429 response body.

        The body is ``{"parameters": {"retry_after": <int>}}``. Fully tolerant:
        a missing key, a non-dict ``parameters``, a non-numeric/negative value,
        or a body that doesn't parse as JSON all fall back to
        ``_FALLBACK_RETRY_AFTER``. The result is clamped to
        ``[_FALLBACK_RETRY_AFTER, retry_after_cap]`` so we never sleep for an
        absurd duration nor for zero. NEVER raises.
        """
        retry_after = _FALLBACK_RETRY_AFTER
        try:
            params = resp.json().get("parameters", {})
            value = params.get("retry_after")
            # Reject bools (a bool is an int) and any non-numeric value.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value)
                if value > 0:
                    retry_after = value
        except (ValueError, AttributeError, TypeError):
            # ValueError: body isn't JSON. AttributeError/TypeError: unexpected
            # shape (e.g. parameters isn't a dict). Keep the prudent default.
            pass
        # Floor at the fallback (so retry_after=0/negative still backs off a bit)
        # and cap so a hostile value can't stall the whole run.
        return max(_FALLBACK_RETRY_AFTER, min(retry_after, self.retry_after_cap))

    def _send(self, text: str, reply_markup: dict | None = None):
        """POST one message. Returns the parsed Telegram ``result`` dict on success
        (so the caller can read ``message_id``), or ``None`` on any failure.

        `reply_markup` is optional and JSON-serialised as the Bot API requires;
        without it the payload is identical to before.

        On HTTP 429 (Too Many Requests) we honour the server's ``retry_after``
        (sleep, capped at ``retry_after_cap``) and re-send, up to ``max_retries``
        total attempts; once they're exhausted we return ``None`` like any other
        failure. Connection/timeout errors and other non-200 statuses behave
        exactly as before (warn, return ``None``). NEVER raises.
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

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                logger.warning("telegram send error: %s", exc)
                return None

            if resp.status_code == 200:
                try:
                    return resp.json().get("result")
                except ValueError:
                    return None

            if resp.status_code == 429:
                # Rate-limited. If we still have attempts left, back off for the
                # server-requested interval and re-send; otherwise give up.
                if attempt < self.max_retries:
                    retry_after = self._retry_after_seconds(resp)
                    logger.warning(
                        "telegram rate-limited (429), retry %d/%d after %.1fs",
                        attempt, self.max_retries - 1, retry_after,
                    )
                    time.sleep(retry_after)
                    continue
                logger.warning(
                    "telegram send failed 429: exhausted %d attempts, dropping message",
                    self.max_retries,
                )
                return None

            # Permanent failure (e.g. 403 — user blocked the bot / chat gone):
            # raise so the per-user fan-out can mark this user blocked and stop
            # sending to them. Distinct from a transient failure (which returns
            # None and is retried on the next run).
            if resp.status_code in PERMANENT_SEND_STATUSES:
                logger.warning("telegram send permanently failed %s: %s",
                               resp.status_code, resp.text[:200])
                raise PermanentSendError(resp.status_code, self.chat_id, resp.text[:200])

            # Any other non-200 status: unchanged behaviour (warn + give up).
            logger.warning("telegram send failed %s: %s", resp.status_code, resp.text[:200])
            return None

        # Defensive: the loop always returns above, but guard against a future
        # max_retries<1 slipping through (the constructor already floors at 1).
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
