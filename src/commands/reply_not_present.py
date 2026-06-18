"""Shared reply formatter for the "items were NOT in the profile" case (/remove_*).

The verb agrees with the count of `items` (singular "is not in your profile" /
plural "are not in your profile"). `items` is assumed non-empty (the caller
guarantees it).
"""

from __future__ import annotations


def format_not_present(label: str, items: list[str]) -> str:
    if len(items) == 1:
        return f"{label}: {items[0]} is not in your profile."
    return f"{label}: {', '.join(items)} are not in your profile."
