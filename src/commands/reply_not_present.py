"""Shared reply formatter for the "items were NOT in the profile" case (/remove_*).

User-facing text is Italian; the verb agrees with the count of `items`
(singular "non è presente" / plural "non sono presenti"). `items` is assumed
non-empty (the caller guarantees it).
"""

from __future__ import annotations


def format_not_present(label: str, items: list[str]) -> str:
    if len(items) == 1:
        return f"{label}: {items[0]} non è presente nel tuo profilo."
    return f"{label}: {', '.join(items)} non sono presenti nel tuo profilo."
