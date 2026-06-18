"""`/remove_keywords` — remove user-interest keywords from the profile overlay.

Keywords are comma-separated; a single keyword may be a multi-word phrase
(e.g. "sparse autoencoder"), so we split ONLY on commas.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_not_present import format_not_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class RemoveKeywordsCommand(Command):
    name = "remove_keywords"
    description = "Rimuovi keyword dal profilo (separate da virgola)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split only on commas; phrases like "sparse autoencoder" stay intact.
        keywords = [kw.strip() for kw in args.split(",")]
        keywords = [kw for kw in keywords if kw]

        if not keywords:
            return "Uso: /remove_keywords keyword1, keyword2, ..."

        removed = store.remove_keywords(keywords)
        # Submitted items not actually removed are the ones absent from the profile.
        notfound = present_items(keywords, removed)

        lines: list[str] = []
        if removed:
            lines.append(format_added("Keyword rimosse", removed))
        if notfound:
            lines.append(format_not_present("Keyword", notfound))
        return "\n".join(lines)
