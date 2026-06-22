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


@dataclass
class RunSummary:
    """Per-run counters, returned by `Pipeline.run` for observability.

    Field names are a contract consumed downstream (main.py) — do not rename.
    """

    fetched: int          # len(raw): items returned by all sources, pre-dedup
    unique: int           # after cross-source dedup
    fresh: int            # after filter_unseen (not processed in a prior run)
    relevant: int         # cleared the relevance/author bar
    alerts: int           # routed to instant alert (never capped)
    digest: int           # digest items actually SENT (after the top-N cap)
    digest_total: int     # relevant digest items BEFORE the cap; >= digest
    scoring_errors: int   # items skipped because the scorer raised


class Pipeline:
    def __init__(
        self,
        sources: list[Source],
        scorer: CombinedScorer,
        store: Store,
        notifier: Notifier,
        profile: UserProfile,
        thresholds: Thresholds,
        digest_cap: int = 5,
    ) -> None:
        self.sources = sources
        self.scorer = scorer
        self.store = store
        self.notifier = notifier
        self.profile = profile
        self.thresholds = thresholds
        # Max digest items per run. Alerts are NEVER capped (a followed author is
        # always notified); the cap applies only to the digest group. A value
        # <= 0 (or larger than the digest size) means "no effective cap".
        self.digest_cap = digest_cap

    def run(self, since: datetime | None, *, mark_seen: bool = True) -> RunSummary:
        raw = self._fetch_all(since)
        unique = _dedup(raw)
        fresh = self.store.filter_unseen(unique)
        logger.info("fetched=%d unique=%d fresh=%d", len(raw), len(unique), len(fresh))

        scored = self._score_all(fresh)
        scoring_errors = len(fresh) - len(scored)
        relevant = [s for s in scored if self._is_relevant(s)]
        alerts = [s for s in relevant if self._is_alert(s)]
        digest = [s for s in relevant if s not in alerts]

        # Cap the DIGEST (only) to the top-N by score, so a quiet day's signal
        # isn't drowned by a flood of marginally-relevant papers. Alerts are
        # deliberately left untouched above: a followed author is always
        # notified — that promise must never be capped.
        digest_top = self._cap_digest(digest)
        logger.info(
            "relevant=%d alerts=%d digest=%d/%d",
            len(relevant), len(alerts), len(digest_top), len(digest),
        )

        self.notifier.notify(alerts, kind="alert")
        self.notifier.notify(digest_top, kind="digest")

        if mark_seen:
            now = datetime.now(timezone.utc)
            # Mark EVERY evaluated item, including digest papers dropped by the
            # top-N cap: they were scored and consciously deprioritised, not
            # missed, so re-surfacing them next run would just re-litigate the
            # same losing comparison. Alerts and relevant-but-low items are
            # marked too, exactly as before. One batched write per run, not one
            # commit per item.
            self.store.mark_seen_many([s.item.canonical_key for s in scored], now)

        return RunSummary(
            fetched=len(raw),
            unique=len(unique),
            fresh=len(fresh),
            relevant=len(relevant),
            alerts=len(alerts),
            digest=len(digest_top),     # actually sent (post-cap)
            digest_total=len(digest),   # relevant digest items (pre-cap)
            scoring_errors=scoring_errors,
        )

    def _score_all(self, items: list[Item]) -> list[ScoredItem]:
        """Score each item, skipping (not raising on) any that the scorer trips on.

        Symmetric with `_fetch_all`: a single bad item must not sink the run. A
        skipped item is NOT added to `scored`, so it is neither delivered nor
        marked seen — a transient scorer failure (e.g. an embedder timeout)
        therefore loses no paper and is retried next run; a persistent one keeps
        emitting warnings for observability.
        """
        scored: list[ScoredItem] = []
        for it in items:
            try:
                scored.append(ScoredItem(it, self.scorer.score(it, self.profile)))
            except Exception as exc:  # one bad item must not sink the run
                logger.warning("scoring %s failed: %s", it.canonical_key, exc)
        return scored

    def _fetch_all(self, since: datetime | None) -> list[Item]:
        items: list[Item] = []
        for source in self.sources:
            try:
                items.extend(source.fetch(since))
            except Exception as exc:  # one bad source must not sink the run
                logger.warning("source %s failed: %s", source.name, exc)
        return items

    def _cap_digest(self, digest: list[ScoredItem]) -> list[ScoredItem]:
        """Return the top-`digest_cap` digest items by descending score.

        Edge cases, all treated as "no effective cap" (return all, still sorted
        so callers get a stable, score-ordered list either way):
          - `digest_cap <= 0`: an explicit "unlimited" sentinel from config.
          - `digest_cap >= len(digest)`: the cap is wider than the group, so the
            slice is a no-op. This is the backward-compatible path: with a cap
            at or above the digest size, every relevant digest item is sent, as
            before. (Ordering by score is new, but the SET sent is unchanged.)

        Ties on `result.total` keep `digest`'s incoming order (Python's sort is
        stable); the input order is the source/fetch order, so this is
        deterministic given the same inputs.
        """
        ordered = sorted(digest, key=lambda s: s.result.total, reverse=True)
        # Guard the slice: `digest_cap <= 0` is the "unlimited" sentinel, but a
        # raw `ordered[:digest_cap]` with a non-positive cap would wrongly drop
        # items (e.g. [:0] -> [], [:-1] -> all-but-last). Skip slicing in that
        # case (and when the cap already covers the whole group) -> send all.
        if self.digest_cap <= 0 or self.digest_cap >= len(ordered):
            return ordered
        return ordered[: self.digest_cap]

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
