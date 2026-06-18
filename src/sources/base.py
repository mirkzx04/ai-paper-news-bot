"""`Source` — abstract base for every content source adapter.

Each concrete source knows how to talk to one API (arXiv, Semantic Scholar,
Bluesky, HF Papers) and maps its responses into `Item`s. The rest of the
pipeline never sees source-specific shapes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.domain.item import Item


class Source(ABC):
    name: str = "source"

    @abstractmethod
    def fetch(self, since: datetime | None) -> list[Item]:
        """Return items published at/after `since` (None = source default window).

        Implementations should be polite to upstream APIs (rate limits) and
        return an empty list on transient failure rather than raising, so one
        flaky source can't sink the whole run.
        """
