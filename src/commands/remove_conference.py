"""`/remove_conference` — remove favorite conferences from the profile overlay.

Counterpart of `/add_conference`. Parses a comma-separated argument string into
conference names and delegates removal to `ProfileStore.remove_conferences`.
Replies in Italian.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_not_present import format_not_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class RemoveConferenceCommand(Command):
    name: str = "remove_conference"
    description: str = "Rimuovi conferenze dal profilo (separate da virgola)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split ONLY on commas: a conference name may contain spaces (e.g. "ICLR Workshop").
        names: list[str] = [part.strip() for part in args.split(",")]
        names = [name for name in names if name]

        if not names:
            return "Uso: /remove_conference NeurIPS[, ICML, ...]"

        removed = store.remove_conferences(names)
        notfound = present_items(names, removed)

        lines: list[str] = []
        if removed:
            lines.append(format_added("Conferenze rimosse", removed))
        if notfound:
            lines.append(format_not_present("Conferenze", notfound))
        return "\n".join(lines)
