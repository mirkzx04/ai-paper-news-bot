"""Tests for `Pipeline.run`: per-item scoring resilience and the `RunSummary`.

Two main guarantees are exercised:

  1. A scorer that raises on a single item must NOT sink the run. That item is
     skipped — not delivered, not marked seen — while every other item is scored
     and routed exactly as before. `RunSummary.scoring_errors` counts the skips.
  2. With all items valid, routing (alert/digest), `mark_seen`, and the
     `RunSummary` counters match the pre-existing behaviour.

All collaborators are in-memory fakes; no network, no DB. Pure stdlib
`unittest` (no pytest dep).
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.item import Item  # noqa: E402
from src.domain.profile import UserProfile  # noqa: E402
from src.notify.base import ScoredItem  # noqa: E402
from src.pipeline import Pipeline, RunSummary, Thresholds  # noqa: E402
from src.scoring.base import ScoreResult  # noqa: E402
from src.store.base import Store  # noqa: E402


# --------------------------------------------------------------------------- #
# In-memory fakes                                                             #
# --------------------------------------------------------------------------- #
class FakeSource:
    """Returns a fixed list of items, ignoring `since`."""

    def __init__(self, items: list[Item], name: str = "fake") -> None:
        self._items = items
        self.name = name

    def fetch(self, since):  # noqa: ANN001 - matches Source.fetch signature
        return list(self._items)


class FakeScorer:
    """Scores by a lookup table keyed on canonical_key.

    A key mapped to a `ScoreResult` returns it; a key mapped to an `Exception`
    instance raises it (to drive the resilience path); an unmapped key returns a
    zero score. Records every key it was *asked* to score so the test can assert
    that the failing item was still attempted (and not silently dropped earlier).
    """

    def __init__(self, table: dict[str, object]) -> None:
        self._table = table
        self.calls: list[str] = []

    def score(self, item: Item, profile: UserProfile) -> ScoreResult:
        self.calls.append(item.canonical_key)
        outcome = self._table.get(item.canonical_key)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, ScoreResult):
            return outcome
        return ScoreResult(total=0.0, breakdown={})


class FakeStore(Store):
    """In-memory seen-set; `filter_unseen` inherited semantics reimplemented.

    Subclasses Store so it inherits `mark_seen_many` (and any future non-abstract
    helper) — keeping the fake from drifting behind the real interface."""

    def __init__(self, already_seen: set[str] | None = None) -> None:
        self.seen: dict[str, datetime] = {}
        self._preseen = already_seen or set()

    def is_seen(self, key: str) -> bool:
        return key in self._preseen or key in self.seen

    def mark_seen(self, key: str, when: datetime) -> None:
        self.seen[key] = when

    def get_meta(self, key: str):  # noqa: ANN201
        return None

    def set_meta(self, key: str, value: str) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def filter_unseen(self, items: list[Item]) -> list[Item]:
        return [it for it in items if not self.is_seen(it.canonical_key)]


class FakeNotifier:
    """Captures notified batches per kind."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, list[ScoredItem]]] = []

    def notify(self, scored: list[ScoredItem], *, kind: str) -> None:
        self.batches.append((kind, list(scored)))

    # convenience accessors -------------------------------------------------- #
    def keys(self, kind: str) -> set[str]:
        out: set[str] = set()
        for k, batch in self.batches:
            if k == kind:
                out.update(s.item.canonical_key for s in batch)
        return out


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def make_item(ext_id: str, *, source: str = "s2", authors=()) -> Item:
    """A minimal non-arXiv item so canonical_key == f'{source}:{ext_id}'.

    Using `s2` ids (no arXiv pattern) keeps canonical keys predictable and
    avoids accidental cross-source dedup collapsing distinct test items.
    """
    return Item(
        source=source,
        external_id=ext_id,
        title=f"Title {ext_id}",
        summary=f"Summary {ext_id}",
        url=f"https://example.org/{ext_id}",
        published=datetime(2026, 6, 1, tzinfo=timezone.utc),
        authors=tuple(authors),
    )


def result(total: float, **breakdown: float) -> ScoreResult:
    return ScoreResult(total=total, breakdown=dict(breakdown))


def build_pipeline(
    items: list[Item],
    table: dict[str, object],
    *,
    store: FakeStore | None = None,
    thresholds: Thresholds | None = None,
    digest_cap: int = 5,
) -> tuple[Pipeline, FakeScorer, FakeStore, FakeNotifier]:
    scorer = FakeScorer(table)
    store = store or FakeStore()
    notifier = FakeNotifier()
    profile = UserProfile(user_id="u1")
    pipe = Pipeline(
        sources=[FakeSource(items)],
        scorer=scorer,            # duck-typed: Pipeline only calls .score(...)
        store=store,
        notifier=notifier,
        profile=profile,
        thresholds=thresholds or Thresholds(digest=0.30, alert=0.60),
        digest_cap=digest_cap,
    )
    return pipe, scorer, store, notifier


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
class ScoringResilienceTest(unittest.TestCase):
    """A single scorer failure is contained; the run still completes."""

    def setUp(self) -> None:
        # Three items: one alert-worthy, one digest-worthy, one that explodes.
        self.alert_item = make_item("alert")     # s2:alert
        self.digest_item = make_item("digest")   # s2:digest
        self.boom_item = make_item("boom")       # s2:boom
        items = [self.alert_item, self.digest_item, self.boom_item]
        table = {
            "s2:alert": result(0.9, keyword=0.9),    # >= alert threshold (0.60)
            "s2:digest": result(0.4, keyword=0.4),   # >= digest (0.30), < alert
            "s2:boom": RuntimeError("SPECTER blew up on a degenerate text"),
        }
        self.pipe, self.scorer, self.store, self.notifier = build_pipeline(items, table)

    def test_run_completes_and_returns_summary(self) -> None:
        # Silence the expected warning so it doesn't clutter test output.
        with self.assertLogs("src.pipeline", level="WARNING") as cm:
            summary = self.pipe.run(since=None)
        self.assertIsInstance(summary, RunSummary)
        # The failing item's key appears in the warning, fetch-style.
        self.assertTrue(any("s2:boom" in line for line in cm.output))

    def test_failing_item_is_skipped_everywhere(self) -> None:
        with self.assertLogs("src.pipeline", level="WARNING"):
            self.pipe.run(since=None)
        # Not delivered in any channel.
        self.assertNotIn("s2:boom", self.notifier.keys("alert"))
        self.assertNotIn("s2:boom", self.notifier.keys("digest"))
        # Not marked seen -> a transient failure can be retried next run.
        self.assertNotIn("s2:boom", self.store.seen)

    def test_other_items_routed_and_marked_as_before(self) -> None:
        with self.assertLogs("src.pipeline", level="WARNING"):
            self.pipe.run(since=None)
        self.assertEqual(self.notifier.keys("alert"), {"s2:alert"})
        self.assertEqual(self.notifier.keys("digest"), {"s2:digest"})
        # The two valid items are marked seen; the failing one is not.
        self.assertEqual(set(self.store.seen), {"s2:alert", "s2:digest"})

    def test_failing_item_was_actually_attempted(self) -> None:
        # Guards against the skip happening *before* scoring (which would mask
        # the resilience path): every fresh item must reach the scorer.
        with self.assertLogs("src.pipeline", level="WARNING"):
            self.pipe.run(since=None)
        self.assertEqual(set(self.scorer.calls), {"s2:alert", "s2:digest", "s2:boom"})

    def test_summary_counts_one_scoring_error(self) -> None:
        with self.assertLogs("src.pipeline", level="WARNING"):
            summary = self.pipe.run(since=None)
        self.assertEqual(summary.scoring_errors, 1)
        self.assertEqual(summary.fetched, 3)
        self.assertEqual(summary.unique, 3)
        self.assertEqual(summary.fresh, 3)
        self.assertEqual(summary.relevant, 2)   # alert + digest items
        self.assertEqual(summary.alerts, 1)
        self.assertEqual(summary.digest, 1)


class AllValidRoutingTest(unittest.TestCase):
    """No failures: routing, mark_seen, and counters match prior behaviour."""

    def setUp(self) -> None:
        self.alert_item = make_item("a1")
        self.author_item = make_item("a2")   # below digest score but author hit
        self.digest_item = make_item("d1")
        self.miss_item = make_item("m1")     # scored but below digest threshold
        items = [self.alert_item, self.author_item, self.digest_item, self.miss_item]
        table = {
            "s2:a1": result(0.8, keyword=0.8),
            # Author hit always alerts regardless of low total (routing rule).
            "s2:a2": result(0.1, keyword=0.1, author=1.0),
            "s2:d1": result(0.45, keyword=0.45),
            "s2:m1": result(0.05, keyword=0.05),
        }
        self.pipe, self.scorer, self.store, self.notifier = build_pipeline(items, table)

    def test_routing_matches_thresholds_and_author_rule(self) -> None:
        summary = self.pipe.run(since=None)
        # alert = total>=0.60 OR author hit -> a1 (high) and a2 (author).
        self.assertEqual(self.notifier.keys("alert"), {"s2:a1", "s2:a2"})
        # digest = relevant AND not alert -> only d1 (m1 is below digest bar).
        self.assertEqual(self.notifier.keys("digest"), {"s2:d1"})
        self.assertEqual(summary.alerts, 2)
        self.assertEqual(summary.digest, 1)
        self.assertEqual(summary.relevant, 3)

    def test_all_evaluated_items_marked_seen(self) -> None:
        self.pipe.run(since=None)
        # mark_seen marks *every* evaluated item, relevant or not (incl. m1).
        self.assertEqual(set(self.store.seen), {"s2:a1", "s2:a2", "s2:d1", "s2:m1"})

    def test_summary_counts_with_no_errors(self) -> None:
        summary = self.pipe.run(since=None)
        self.assertEqual(summary.scoring_errors, 0)
        self.assertEqual(summary.fetched, 4)
        self.assertEqual(summary.unique, 4)
        self.assertEqual(summary.fresh, 4)
        self.assertEqual(
            summary,
            # digest_total added to the contract; here digest==digest_total
            # because only 1 digest item exists (well under the default cap=5).
            RunSummary(fetched=4, unique=4, fresh=4, relevant=3,
                       alerts=2, digest=1, digest_total=1, scoring_errors=0),
        )

    def test_dry_run_does_not_mark_seen_but_still_routes(self) -> None:
        # mark_seen=False (dry run) must not persist anything yet still deliver.
        summary = self.pipe.run(since=None, mark_seen=False)
        self.assertEqual(self.store.seen, {})
        self.assertEqual(self.notifier.keys("alert"), {"s2:a1", "s2:a2"})
        self.assertEqual(summary.alerts, 2)


class EdgeCountsTest(unittest.TestCase):
    """Dedup and already-seen filtering feed the summary counters correctly."""

    def test_dedup_and_seen_reflected_in_summary(self) -> None:
        # Two raw items collapse to one via canonical_key; another is pre-seen.
        dup_a = make_item("dup")
        dup_b = make_item("dup")          # same key -> deduped away
        already = make_item("old")
        fresh_one = make_item("new")
        items = [dup_a, dup_b, already, fresh_one]
        table = {
            "s2:dup": result(0.7, keyword=0.7),
            "s2:new": result(0.4, keyword=0.4),
            # s2:old would score high, but it's pre-seen so never scored.
            "s2:old": result(0.9, keyword=0.9),
        }
        store = FakeStore(already_seen={"s2:old"})
        pipe, scorer, _, notifier = build_pipeline(items, table, store=store)

        summary = pipe.run(since=None)
        self.assertEqual(summary.fetched, 4)   # raw, pre-dedup
        self.assertEqual(summary.unique, 3)    # dup collapsed
        self.assertEqual(summary.fresh, 2)     # 'old' filtered as seen
        self.assertEqual(summary.scoring_errors, 0)
        # The pre-seen item is never even scored.
        self.assertNotIn("s2:old", scorer.calls)
        self.assertEqual(notifier.keys("alert"), {"s2:dup"})
        self.assertEqual(notifier.keys("digest"), {"s2:new"})


class DigestCapTest(unittest.TestCase):
    """The top-N digest cap: bounds the digest, never the alerts."""

    def test_only_top_n_digest_sent_and_they_are_the_right_ones(self) -> None:
        # 8 digest-worthy items (>=0.30, <0.60), distinct scores; cap at 5.
        # Expect the 5 highest-scoring to be sent, the 3 lowest dropped.
        scores = {
            "s2:d1": 0.31, "s2:d2": 0.58, "s2:d3": 0.42, "s2:d4": 0.55,
            "s2:d5": 0.39, "s2:d6": 0.50, "s2:d7": 0.34, "s2:d8": 0.47,
        }
        items = [make_item(k.split(":")[1]) for k in scores]
        table = {k: result(v, keyword=v) for k, v in scores.items()}
        pipe, _, store, notifier = build_pipeline(items, table, digest_cap=5)

        summary = pipe.run(since=None)

        # Top 5 by score: d2(.58) d4(.55) d6(.50) d8(.47) d3(.42).
        self.assertEqual(
            notifier.keys("digest"),
            {"s2:d2", "s2:d4", "s2:d6", "s2:d8", "s2:d3"},
        )
        # The 3 below the cut are NOT delivered.
        for dropped in ("s2:d5", "s2:d7", "s2:d1"):
            self.assertNotIn(dropped, notifier.keys("digest"))
        # Summary distinguishes sent (5) from total relevant digest (8).
        self.assertEqual(summary.digest, 5)
        self.assertEqual(summary.digest_total, 8)
        self.assertEqual(summary.relevant, 8)
        self.assertEqual(summary.alerts, 0)
        # mark_seen marks ALL evaluated items, incl. the 3 dropped by the cap.
        self.assertEqual(set(store.seen), set(scores))

    def test_alerts_are_never_capped(self) -> None:
        # 7 alerts + 9 digest, cap=5 -> all 7 alerts, only 5 digest.
        alert_scores = {f"s2:al{i}": 0.70 + i * 0.01 for i in range(7)}
        digest_scores = {f"s2:dg{i}": 0.31 + i * 0.02 for i in range(9)}
        items = [make_item(k.split(":")[1]) for k in (*alert_scores, *digest_scores)]
        table = {k: result(v, keyword=v) for k, v in alert_scores.items()}
        table |= {k: result(v, keyword=v) for k, v in digest_scores.items()}
        pipe, _, store, notifier = build_pipeline(items, table, digest_cap=5)

        summary = pipe.run(since=None)

        # Every alert is delivered — the cap must not touch this channel.
        self.assertEqual(notifier.keys("alert"), set(alert_scores))
        self.assertEqual(summary.alerts, 7)
        # Digest is capped to 5 of the 9 relevant ones.
        self.assertEqual(len(notifier.keys("digest")), 5)
        self.assertEqual(summary.digest, 5)
        self.assertEqual(summary.digest_total, 9)
        self.assertEqual(summary.relevant, 16)
        # All 16 evaluated items marked seen (alerts + every digest, capped or not).
        self.assertEqual(set(store.seen), set(alert_scores) | set(digest_scores))

    def test_author_alert_below_score_is_never_capped(self) -> None:
        # An author-hit item with a tiny score must still alert (not be treated
        # as a low-priority digest item the cap could drop). 6 digest + 1 author
        # alert, cap=5: author always alerts; digest capped to 5.
        digest_scores = {f"s2:dg{i}": 0.31 + i * 0.03 for i in range(6)}
        items = [make_item(k.split(":")[1]) for k in digest_scores]
        author = make_item("auth")
        items.append(author)
        table = {k: result(v, keyword=v) for k, v in digest_scores.items()}
        table["s2:auth"] = result(0.05, keyword=0.05, author=1.0)  # author hit
        pipe, _, _, notifier = build_pipeline(items, table, digest_cap=5)

        summary = pipe.run(since=None)

        self.assertEqual(notifier.keys("alert"), {"s2:auth"})
        self.assertEqual(summary.alerts, 1)
        self.assertEqual(summary.digest, 5)
        self.assertEqual(summary.digest_total, 6)

    def test_below_cap_is_backward_compatible(self) -> None:
        # With <= N digest items, every relevant digest item is sent and the
        # summary's digest == digest_total: identical to pre-cap behaviour.
        scores = {"s2:d1": 0.40, "s2:d2": 0.50, "s2:a1": 0.80}
        items = [make_item(k.split(":")[1]) for k in scores]
        table = {k: result(v, keyword=v) for k, v in scores.items()}
        pipe, _, store, notifier = build_pipeline(items, table, digest_cap=5)

        summary = pipe.run(since=None)

        self.assertEqual(notifier.keys("digest"), {"s2:d1", "s2:d2"})
        self.assertEqual(notifier.keys("alert"), {"s2:a1"})
        self.assertEqual(summary.digest, 2)
        self.assertEqual(summary.digest_total, 2)   # no cap effect
        self.assertEqual(set(store.seen), set(scores))

    def test_exactly_n_digest_all_sent(self) -> None:
        # Boundary: digest size == cap -> all sent, no drop (cap is inclusive).
        scores = {f"s2:d{i}": 0.30 + i * 0.05 for i in range(5)}
        items = [make_item(k.split(":")[1]) for k in scores]
        table = {k: result(v, keyword=v) for k, v in scores.items()}
        pipe, _, _, notifier = build_pipeline(items, table, digest_cap=5)

        summary = pipe.run(since=None)
        self.assertEqual(notifier.keys("digest"), set(scores))
        self.assertEqual(summary.digest, 5)
        self.assertEqual(summary.digest_total, 5)

    def test_non_positive_cap_means_unlimited(self) -> None:
        # digest_cap <= 0 is the "no cap" sentinel: every digest item is sent
        # (must NOT be read as ordered[:0] -> empty).
        scores = {f"s2:d{i}": 0.30 + i * 0.03 for i in range(8)}
        items = [make_item(k.split(":")[1]) for k in scores]
        table = {k: result(v, keyword=v) for k, v in scores.items()}
        for cap in (0, -1):
            pipe, _, _, notifier = build_pipeline(items, table, digest_cap=cap)
            summary = pipe.run(since=None)
            self.assertEqual(notifier.keys("digest"), set(scores), f"cap={cap}")
            self.assertEqual(summary.digest, 8, f"cap={cap}")
            self.assertEqual(summary.digest_total, 8, f"cap={cap}")


if __name__ == "__main__":
    unittest.main()
