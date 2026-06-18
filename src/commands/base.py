"""`Command` — abstract base for a Telegram slash-command handler.

Each command parses its argument string, mutates the `ProfileStore`, and returns
a user-facing reply (in Italian — the user writes Italian). `name` is the
Telegram command WITHOUT the leading slash and WITHOUT hyphens (Telegram splits
commands on non-word chars), e.g. "add_author".
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.store.profile_store import ProfileStore


class Command(ABC):
    name: str = "command"
    description: str = ""

    @abstractmethod
    def handle(self, args: str, store: ProfileStore) -> str:
        """Process `args`, mutate `store`, return the reply text to send back."""
