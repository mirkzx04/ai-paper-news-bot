"""`/add_keywords` — add user-interest keywords to the profile overlay.

Keywords are comma-separated; a single keyword may be a multi-word phrase
(e.g. "sparse autoencoder"), so we split ONLY on commas.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_present import format_already_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class AddKeywordsCommand(Command):
    name = "add_keywords"
    description = "Add keywords you're interested in (comma-separated)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split only on commas; phrases like "sparse autoencoder" stay intact.
        keywords = [kw.strip() for kw in args.split(",")]
        keywords = [kw for kw in keywords if kw]

        if not keywords:
            return "Usage: /add_keywords keyword1, keyword2, ..."

        added = store.add_keywords(keywords)
        present = present_items(keywords, added)

        lines: list[str] = []
        if added:
            lines.append(format_added("Keywords added", added))
        if present:
            lines.append(format_already_present("Keywords", present))
        return "\n".join(lines)
