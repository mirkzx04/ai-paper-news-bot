"""Console notifier — prints matches in the labeled format (dev/testing).

Mirrors the Telegram message layout in plain text: same labeled paragraphs,
same ordering, scoreless. No HTML and no escaping here — this is a dev sink.
"""

from __future__ import annotations

from src.notify.base import Notifier, ScoredItem
from src.notify.telegram_notifier import _short_summary


class ConsoleNotifier(Notifier):
    def __init__(self, field_classifier=None) -> None:
        # Duck-typed: .classify(item) -> list[str].
        self.field_classifier = field_classifier

    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        if not scored:
            return
        header = "🔔 ALERT" if kind == "alert" else "📰 DIGEST"
        print(f"\n{'=' * 70}\n{header}  ({len(scored)} item)\n{'=' * 70}")
        for s in sorted(scored, key=lambda x: x.result.total, reverse=True):
            print(self._format(s, kind))

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
            f"📄 Title: {title_prefix}{item.title}",
        ]
        if authors:
            paragraphs.append(f"👤 Authors: {authors}")
        if fields:
            paragraphs.append(f"🏷 Field: {', '.join(fields)}")
        if item.venue:
            paragraphs.append(f"🎓 Venue: {item.venue}")
        # Release date — always present (every item has a tz-aware `published`).
        paragraphs.append(f"📅 Date: {item.published:%Y-%m-%d}")
        if summary:
            paragraphs.append(f"📝 Summary: {summary}")
        paragraphs.append(f"🔗 {item.url}")

        return "\n" + "\n\n".join(paragraphs) + "\n"
