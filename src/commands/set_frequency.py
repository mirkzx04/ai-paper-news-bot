"""`/set_frequency` — choose how often the digest is delivered.

The user picks one of three cadences, stored as a canonical string on the
`ProfileStore` (`2x_daily` | `daily` | `weekly`). The coordinator in `main.py`
reads that preference to decide whether a given cron run should actually send;
this command only parses the user's input and persists the choice.

Input is parsed user-friendly: short forms and Italian/English synonyms all map
onto a canonical value (case-insensitive). Unrecognized input is answered with
the list of valid options — it never raises.
"""

from __future__ import annotations

from src.commands.base import Command
from src.store.profile_store import ProfileStore

# Human-readable label per canonical value (used in confirmations/listings).
_LABELS = {
    "2x_daily": "2× al giorno",
    "daily": "1× al giorno",
    "weekly": "1× a settimana",
}

# Synonym -> canonical. Keys are lowercased; lookup lowercases the input too.
# Covers the required forms (e.g. "2x"/"twice"/"due" -> "2x_daily") plus a few
# natural variants. Keep every key UNIQUE: a synonym must map to one cadence.
_SYNONYMS = {
    # 2x_daily
    "2x_daily": "2x_daily",
    "2x": "2x_daily",
    "2xday": "2x_daily",
    "2xdaily": "2x_daily",
    "twice": "2x_daily",
    "2": "2x_daily",
    "due": "2x_daily",
    # daily
    "daily": "daily",
    "1x": "daily",
    "1": "daily",
    "day": "daily",
    "giorno": "daily",
    "giornaliero": "daily",
    "giornaliera": "daily",
    # weekly
    "weekly": "weekly",
    "week": "weekly",
    "7": "weekly",
    "settimana": "weekly",
    "sett": "weekly",
    "settimanale": "weekly",
}


def _options_block() -> str:
    """The list of accepted cadences with example usage, for help/error replies."""
    return (
        "Opzioni disponibili:\n"
        "• 2× al giorno — es. /set_frequency 2x  (canonico: 2x_daily)\n"
        "• 1× al giorno — es. /set_frequency daily  (canonico: daily)\n"
        "• 1× a settimana — es. /set_frequency weekly  (canonico: weekly)"
    )


class SetFrequencyCommand(Command):
    name = "set_frequency"
    description = "Choose how often you receive the digest (2x_daily / daily / weekly)"

    def handle(self, args: str, store: ProfileStore) -> str:
        raw = args.strip()

        # No argument: show the current cadence plus how to change it.
        if not raw:
            current = store.digest_frequency
            label = _LABELS.get(current, current)
            return (
                f"Frequenza attuale del digest: {label} ({current}).\n\n"
                f"{_options_block()}"
            )

        # Normalize: collapse internal whitespace and lowercase before lookup,
        # so "2X", "  2x  " and "2x" all match.
        key = " ".join(raw.split()).lower()
        canonical = _SYNONYMS.get(key)
        if canonical is None:
            return (
                f"Frequenza non riconosciuta: «{raw}».\n\n"
                f"{_options_block()}"
            )

        # The store re-validates; given a canonical value this always succeeds,
        # but we honor its boolean contract rather than assume.
        if not store.set_digest_frequency(canonical):
            return (
                f"Impossibile impostare la frequenza: «{raw}».\n\n"
                f"{_options_block()}"
            )

        return f"✅ Frequenza impostata: {_LABELS[canonical]} ({canonical})"
