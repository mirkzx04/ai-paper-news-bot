"""`/add_author` — add favourite authors to the user's profile overlay.

Authors are passed as a comma-separated list (a single name may contain spaces,
e.g. "Neel Nanda"), so we split ONLY on commas. New names are persisted via
`ProfileStore.add_authors`, which dedups case-insensitively and returns the
names that were actually added; we use that to build a precise Italian reply.
"""

from __future__ import annotations

from src.commands.base import Command
from src.commands.reply_added import format_added
from src.commands.reply_present import format_already_present
from src.commands.util import present_items
from src.store.profile_store import ProfileStore


class AddAuthorCommand(Command):
    name = "add_author"
    description = "Add favorite authors (comma-separated)"

    def handle(self, args: str, store: ProfileStore) -> str:
        # Split on commas only (names may contain spaces); drop empty entries.
        names = [part.strip() for part in args.split(",")]
        names = [name for name in names if name]
        if not names:
            return "Usage: /add_author Author Name[, Another Author]"

        added = store.add_authors(names)
        present = present_items(names, added)

        lines: list[str] = []
        if added:
            lines.append(format_added("Authors added", added))
        if present:
            lines.append(format_already_present("Authors", present))
        return "\n".join(lines)
