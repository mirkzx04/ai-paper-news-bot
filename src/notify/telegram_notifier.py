"""Telegram notifier — sends each item as a message via the Bot API.

Stateless `sendMessage` calls (just HTTP POST), which is exactly what the
GitHub Actions deployment needs — no webhook, no long-running process. One
message per item keeps it forward-compatible with the Phase 3 feedback loop,
where each message gets inline 👍/👎 buttons.
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

from src.notify.base import Notifier, ScoredItem

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

# Short summary budget: keep notifications skimmable.
_SUMMARY_MAX_CHARS = 300
_SUMMARY_MAX_SENTENCES = 2


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
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.throttle = throttle
        self.timeout = timeout
        self.field_classifier = field_classifier

    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        if not scored:
            return
        ordered = sorted(scored, key=lambda x: x.result.total, reverse=True)
        for s in ordered:
            self._send(self._format(s, kind))
            time.sleep(self.throttle)

    def _send(self, text: str) -> bool:
        url = _API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": False,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("telegram send error: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning("telegram send failed %s: %s", resp.status_code, resp.text[:200])
            return False
        return True

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
