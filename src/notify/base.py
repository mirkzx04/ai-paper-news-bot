"""`Notifier` — delivery boundary (console for dev, Telegram for the real bot)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.domain.item import Item
from src.scoring.base import ScoreResult


@dataclass
class ScoredItem:
    item: Item
    result: ScoreResult


class Notifier(ABC):
    @abstractmethod
    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        """Deliver a batch of items. `kind` is 'alert' (instant) or 'digest'."""
