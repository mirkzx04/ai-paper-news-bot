"""`/add_conference` — add favorite conferences to the profile overlay.

Parses a comma-separated argument string into conference names and delegates
deduplication to `ProfileStore.add_conferences`. Replies in Italian.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_present import format_already_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class AddConferenceCommand(Command):
    name: str = "add_conference"
    description: str = "Aggiungi conferenze preferite (separate da virgola)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split ONLY on commas: a conference name may contain spaces (e.g. "ICLR Workshop").
        names: list[str] = [part.strip() for part in args.split(",")]
        names = [name for name in names if name]

        if not names:
            return "Uso: /add_conference NeurIPS[, ICML, ...]"

        added = store.add_conferences(names)
        present = present_items(names, added)

        lines: list[str] = []
        if added:
            lines.append(format_added("Conferenze aggiunte", added))
        if present:
            lines.append(format_already_present("Conferenze", present))
        return "\n".join(lines)
