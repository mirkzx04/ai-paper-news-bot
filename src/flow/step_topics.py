"""Topics step of the /creare_profile onboarding flow.

Product decision: a user's "topic of interest" is stored as a KEYWORD (so it
actually feeds the scorer), NOT as a field-classifier topic. This handler
therefore routes the parsed topics straight into ``ProfileStore.add_keywords``.
"""

from __future__ import annotations

from src.commands.reply_added import format_added
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


def _parse_topics(text: str) -> list[str]:
    """Split the user's message into topics.

    Accept BOTH newline- and comma-separated input: split on newlines first,
    then on commas. A topic may be a multi-word phrase (e.g. "mixture of
    experts"), so we never split on spaces. Each piece is stripped and empties
    are dropped.
    """
    topics: list[str] = []
    for line in text.split("\n"):
        for piece in line.split(","):
            value = piece.strip()
            if value:
                topics.append(value)
    return topics


def handle_topics(text: str, profile_store: ProfileStore) -> str:
    """Handle the topics step: persist topics as keywords, reply in Italian."""
    topics = _parse_topics(text)
    if not topics:
        # No usable topic found -> usage hint.
        return (
            "Send me one or more topics you're interested in, separated by commas or "
            "new lines (e.g. mixture of experts, interpretability)."
        )

    added = profile_store.add_keywords(topics)
    if added:
        reply = format_added("Topics added", added)
    else:
        reply = "No new topics added."

    # Topics submitted but already present in the profile.
    already = present_items(topics, added)
    if already:
        reply += "\nAlready in your profile: " + ", ".join(already)
    return reply
