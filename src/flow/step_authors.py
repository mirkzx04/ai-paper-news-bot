"""Authors step of the /creare_profile onboarding flow.

The user sends a free-text message listing their favourite author names. We
accept both newline-separated and comma-separated input (e.g. a mix of
"A, B\nC"), since names may contain spaces ("Neel Nanda") we never split on
spaces. Parsed names are handed to the ProfileStore, and the Italian reply
distinguishes newly-added authors from ones already in the profile.
"""

from __future__ import annotations

from src.commands.reply_added import format_added
from src.commands.util import present_items


def _parse_names(text: str) -> list[str]:
    """Split free text into author names: newlines first, then commas.

    Whitespace around each name is stripped and empty fragments are dropped.
    Spaces inside a name are preserved (no split on spaces).
    """
    names: list[str] = []
    for line in text.split("\n"):
        for part in line.split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def handle_authors(text: str, profile_store) -> str:
    """Handle the authors onboarding step; return the Italian reply text."""
    names = _parse_names(text)
    if not names:
        # No usable name parsed -> ask the user for at least one author.
        return (
            "Send me at least one favorite author. "
            "You can separate them with commas or new lines "
            "(e.g. \"Neel Nanda, Chris Olah\")."
        )

    added = profile_store.add_authors(names)
    already_present = present_items(names, added)

    lines: list[str] = []
    if added:
        lines.append(format_added("Authors added", added))
    if already_present:
        lines.append(f"Already in your profile: {', '.join(already_present)}")
    return "\n".join(lines)
