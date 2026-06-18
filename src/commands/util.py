"""Small helpers shared by the /add_* command handlers."""

from __future__ import annotations


def present_items(submitted: list[str], added: list[str]) -> list[str]:
    """The submitted items that were NOT newly added (i.e. already in the profile).

    Case-insensitive, deduplicated, original order/casing preserved — so a reply
    never lists the same already-present item twice.
    """
    added_lower = {a.lower() for a in added}
    out: list[str] = []
    seen: set[str] = set()
    for item in submitted:
        low = item.lower()
        if low in added_lower or low in seen:
            continue
        seen.add(low)
        out.append(item)
    return out
