from __future__ import annotations


def format_already_present(label: str, items: list[str]) -> str:
    """Build the Italian reply for items that were already present in the profile.

    User-facing text is in Italian. The verb agrees with the count of ``items``:
    singular ("è già presente") for exactly one item, plural ("sono già
    presenti") for two or more. ``items`` is assumed non-empty (caller guarantees it).
    """
    if len(items) == 1:
        return f"{label}: {items[0]} è già presente nel tuo profilo."
    return f"{label}: {', '.join(items)} sono già presenti nel tuo profilo."
