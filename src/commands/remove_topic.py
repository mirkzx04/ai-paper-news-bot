"""`/remove_topic` — delete a research topic, or remove keywords from one.

Syntax: `/remove_topic Nome del topic[: keyword1, keyword2]`. The topic name may
contain spaces, so we split the argument string on the FIRST colon only. No
colon (or no keywords) => the WHOLE topic is removed.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_not_present import format_not_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class RemoveTopicCommand(Command):
    name = "remove_topic"
    description = "Remove a topic, or keywords from a topic. Usage: /remove_topic Name[: kw1, kw2]"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split on the FIRST colon: left -> topic name (may contain spaces),
        # right -> comma-separated keywords. No colon => whole arg is the name.
        if ":" in args:
            name_part, keywords_part = args.split(":", 1)
        else:
            name_part, keywords_part = args, ""

        name = name_part.strip()
        keywords = [kw.strip() for kw in keywords_part.split(",") if kw.strip()]

        if not name:
            return "Usage: /remove_topic Topic name[: keyword1, keyword2]"

        outcome, removed = store.remove_topic(name, keywords)

        if outcome == "not_found":
            return f"Topic «{name}» is not in your profile."
        if outcome == "topic_removed":
            return f"Topic «{name}» removed."
        # outcome == "keywords_removed"
        if removed:
            return format_added(f"Keywords removed from topic «{name}»", removed)
        # None of the submitted keywords were in the topic.
        return format_not_present(f"Topic «{name}»", present_items(keywords, []))
