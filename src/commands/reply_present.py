from __future__ import annotations


def format_already_present(label: str, items: list[str]) -> str:
    """Build the reply for items that were already present in the profile.

    The verb agrees with the count of ``items``: singular ("is already in your
    profile") for exactly one item, plural ("are already in your profile") for
    two or more. ``items`` is assumed non-empty (caller guarantees it).
    """
    if len(items) == 1:
        return f"{label}: {items[0]} is already in your profile."
    return f"{label}: {', '.join(items)} are already in your profile."
