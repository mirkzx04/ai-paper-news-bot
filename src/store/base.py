"""`Store` — persistence boundary (seen-ids now; profile + telegram offset later).

Abstracted so the local dev backend (SQLite) and the CI backend (JSON committed
to the `state` branch) are interchangeable behind one interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.domain.item import Item


class Store(ABC):
    @abstractmethod
    def is_seen(self, key: str) -> bool: ...

    @abstractmethod
    def mark_seen(self, key: str, when: datetime) -> None: ...

    def mark_seen_many(self, keys: list[str], when: datetime) -> None:
        """Mark several keys seen at once. Default is a loop; SQLite overrides it
        with a single batched transaction. Backends needn't reimplement to stay
        correct — only to be fast."""
        for key in keys:
            self.mark_seen(key, when)

    @abstractmethod
    def get_meta(self, key: str) -> str | None:
        """Read a small key/value (e.g. the Telegram update offset)."""

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None:
        """Write a small key/value."""

    @abstractmethod
    def close(self) -> None: ...

    def filter_unseen(self, items: list[Item]) -> list[Item]:
        """Drop items already processed in a previous run (by canonical key)."""
        return [it for it in items if not self.is_seen(it.canonical_key)]
