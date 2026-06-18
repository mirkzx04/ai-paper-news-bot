"""`/add_topic` — create or extend a research topic (a named keyword group).

Syntax: `/add_topic Nome del topic: keyword1, keyword2`. The topic name may
contain spaces, so we split the argument string on the FIRST colon only.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_present import format_already_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class AddTopicCommand(Command):
    name = "add_topic"
    description = "Add or extend a research topic. Usage: /add_topic Name: kw1, kw2"

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
            return "Usage: /add_topic Topic name: keyword1, keyword2"

        created, added = store.add_topic(name, keywords)

        # A topic has two axes (the topic itself + its keywords), so the wording
        # is adapted but reuses the same two formatters as the other commands.
        if created and added:
            return format_added(f"New topic «{name}» with keywords", added)
        if created:
            return f"New topic «{name}» added."
        if added:
            return format_added(f"Keywords added to topic «{name}»", added)
        if keywords:
            return format_already_present(f"Topic «{name}»", present_items(keywords, []))
        return f"Topic «{name}» is already in your profile."
