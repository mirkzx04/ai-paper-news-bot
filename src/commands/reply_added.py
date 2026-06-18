from __future__ import annotations


def format_added(label: str, items: list[str]) -> str:
    """Build the user-facing reply line for the "items were ADDED" case.

    Shared by the bot's ``/add_*`` commands. The wording is supplied by the
    caller through ``label`` (e.g. "Conferences added"); this function only
    joins the added ``items`` into a single comma-separated line. ``items`` is
    assumed non-empty (guaranteed by the caller).
    """
    return f"{label}: {', '.join(items)}"
