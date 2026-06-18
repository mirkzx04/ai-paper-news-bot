"""`/remove_author` — remove authors from the user's profile overlay.

Counterpart of `/add_author`. Authors are passed as a comma-separated list (a
single name may contain spaces, e.g. "Neel Nanda"), so we split ONLY on commas.
Names are removed via `ProfileStore.remove_authors`, which matches
case-insensitively and returns the names that were actually removed (in their
stored casing); we use that to build a precise Italian reply.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_not_present import format_not_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class RemoveAuthorCommand(Command):
    name = "remove_author"
    description = "Remove authors from your profile (comma-separated)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split on commas only (names may contain spaces); drop empty entries.
        names = [part.strip() for part in args.split(",")]
        names = [name for name in names if name]
        if not names:
            return "Usage: /remove_author Author Name[, Another Author]"

        removed = store.remove_authors(names)
        notfound = present_items(names, removed)

        lines: list[str] = []
        if removed:
            lines.append(format_added("Authors removed", removed))
        if notfound:
            lines.append(format_not_present("Authors", notfound))
        return "\n".join(lines)
