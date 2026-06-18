"""Pipeline orchestrator: fetch -> dedup -> score -> route -> notify -> persist.

Routing rule (kept here, not in the scorer, so it stays explicit):
  - relevant   = total >= digest_threshold  OR  followed-author match
  - alert      = relevant AND (total >= alert_threshold OR followed-author match)
  - digest     = relevant AND not alert
A followed author always alerts — that's the point of following someone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.domain.item import Item
from src.domain.profile import UserProfile
from src.notify.base import Notifier, ScoredItem
from src.scoring.combined import CombinedScorer
from src.sources.base import Source
from src.store.base import Store

logger = logging.getLogger(__name__)


@dataclass
class Thresholds:
    digest: float = 0.30
    alert: float = 0.60


class Pipeline:
    def __init__(
        self,
        sources: list[Source],
        scorer: CombinedScorer,
        store: Store,
        notifier: Notifier,
        profile: UserProfile,
        thresholds: Thresholds,
    ) -> None:
        self.sources = sources
        self.scorer = scorer
        self.store = store
        self.notifier = notifier
        self.profile = profile
        self.thresholds = thresholds

    def run(self, since: datetime | None, *, mark_seen: bool = True) -> None:
        raw = self._fetch_all(since)
        unique = _dedup(raw)
        fresh = self.store.filter_unseen(unique)
        logger.info("fetched=%d unique=%d fresh=%d", len(raw), len(unique), len(fresh))

        scored = [ScoredItem(it, self.scorer.score(it, self.profile)) for it in fresh]
        relevant = [s for s in scored if self._is_relevant(s)]
        alerts = [s for s in relevant if self._is_alert(s)]
        digest = [s for s in relevant if s not in alerts]
        logger.info("relevant=%d alerts=%d digest=%d", len(relevant), len(alerts), len(digest))

        self.notifier.notify(alerts, kind="alert")
        self.notifier.notify(digest, kind="digest")

        if mark_seen:
            now = datetime.now(timezone.utc)
            for s in scored:  # mark everything evaluated, relevant or not
                self.store.mark_seen(s.item.canonical_key, now)

    def _fetch_all(self, since: datetime | None) -> list[Item]:
        items: list[Item] = []
        for source in self.sources:
            try:
                items.extend(source.fetch(since))
            except Exception as exc:  # one bad source must not sink the run
                logger.warning("source %s failed: %s", source.name, exc)
        return items

    def _is_relevant(self, s: ScoredItem) -> bool:
        return s.result.total >= self.thresholds.digest or _author_hit(s)

    def _is_alert(self, s: ScoredItem) -> bool:
        return s.result.total >= self.thresholds.alert or _author_hit(s)


def _author_hit(s: ScoredItem) -> bool:
    return s.result.breakdown.get("author", 0.0) >= 1.0


def _dedup(items: list[Item]) -> list[Item]:
    """Keep first occurrence per canonical key (within this run)."""
    seen: set[str] = set()
    out: list[Item] = []
    for it in items:
        if it.canonical_key in seen:
            continue
        seen.add(it.canonical_key)
        out.append(it)
    return out
