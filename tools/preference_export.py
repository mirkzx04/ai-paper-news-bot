"""Export the preference dataset (`data/preferences.jsonl`) as a labelled set.

This bridges the append-only `PreferenceDataset` to the eval protocol used by
`tools/eval_ranking.py`, so the ranker can eventually be measured against the
user's *real* preferences instead of a hand-curated, hard-coded list.

LABEL STRENGTH — read this before trusting the output
-----------------------------------------------------
The only *strong, paper-level, two-sided* label is a 👍/👎 ``vote`` event, now
emitted by the feedback loop (see `src.telegram_poller`). The exporter returns,
in decreasing label strength:

  - ``vote_positives`` / ``vote_negatives`` : canonical_keys the user explicitly
                            voted 👍 / 👎 — the strong, two-sided, paper-level
                            labels. Net-state: a flipped or toggled-off vote
                            ("none") is resolved to its final signal.
  - ``seed_positives``    : arXiv ids the user saved as seed papers
                            (``profile_add`` / ``kind="seed"``) — strong-ish
                            positives at the paper level.
  - ``weak_pos_terms``    : authors / keywords / topic-keywords the user added.
                            These are *weak* positives: they describe what the
                            user likes thematically, not specific judged papers,
                            and there is NO negative counterpart (a removal is a
                            withdrawal of interest, not a "this paper is bad").
  - ``weak_negatives``    : canonical_keys SHOWN to the user (``impression``
                            events) but never voted — a WEAK "seen and ignored"
                            negative, for EVAL ONLY. Distinct from
                            ``vote_negatives`` and, like the embedding loop,
                            never used to score: an unvoted impression must not
                            penalise similar papers.

`eval_ranking.py` keeps its hard-coded curated set as the default; pass
``--preferences`` (optionally with a path) to additionally fold in the seed
positives exported here. See `eval_ranking.main`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store.preference_dataset import PreferenceDataset


def export_labels(dataset: PreferenceDataset) -> dict:
    """Build a labelled set from a `PreferenceDataset`.

    Returns a dict with:
        ``seed_positives``  : list[str]  — arXiv ids saved as seeds (strong-ish +)
        ``weak_pos_terms``  : dict[str, list[str]] — {author|keyword|topic: [...]}
                              weak thematic positives (no negative counterpart)
        ``vote_positives``  : list[str]  — canonical_keys voted 👍 (net state)
        ``vote_negatives``  : list[str]  — canonical_keys voted 👎 (net state)
        ``weak_negatives``  : list[str]  — canonical_keys SHOWN (``impression``)
                              but never explicitly voted. A WEAK negative signal
                              for eval only: "seen and ignored" ≠ "👎". Kept
                              strictly DISTINCT from ``vote_negatives`` (papers
                              with any 👍/👎/none vote are excluded here) and,
                              like the embedding loop, impressions never feed
                              scoring — they live here purely for analysis.

    De-dups while preserving first-seen order. Reflects the CURRENT net state:
    an item later removed via ``profile_remove`` is dropped from the positives
    (a withdrawn interest should not count as a positive label), and a vote
    later toggled off (``signal=="none"``) drops out of both vote lists.
    """
    # Track net membership per (kind) honouring add/remove ordering.
    seed_pos: list[str] = []
    weak: dict[str, list[str]] = {"author": [], "keyword": [], "topic": []}

    def _add(bucket: list[str], value: str) -> None:
        if value not in bucket:
            bucket.append(value)

    def _remove(bucket: list[str], value: str) -> None:
        if value in bucket:
            bucket.remove(value)

    for ev in dataset.events(types=["profile_add", "profile_remove"]):
        kind = ev.get("kind")
        value = ev.get("value")
        if not isinstance(value, str) or not value:
            continue
        adding = ev.get("type") == "profile_add"
        if kind == "seed":
            (_add if adding else _remove)(seed_pos, value)
        elif kind in weak:
            (_add if adding else _remove)(weak[kind], value)
        # `conference` is intentionally not exported as a label: a venue is a
        # filter, not a topical preference over paper content.

    # Strong, two-sided labels from votes. Net-state per key (last vote wins):
    # we track the most recent signal AND its first-seen position so the output
    # keeps first-seen order while reflecting the FINAL signal. A key whose last
    # vote is "none" (toggle-off / withdrawn) is excluded from both lists — it is
    # no longer a label, mirroring the embedding loop's net-state. (This also
    # correctly handles an up->down flip: the key lands only in the negatives,
    # not in both — a fix vs. the prior naive add-on-each-event logic, made
    # necessary by introducing the "none" signal.)
    last_signal: dict[str, str] = {}
    order: list[str] = []
    for ev in dataset.events(types=["vote"]):
        key = ev.get("canonical_key")
        signal = ev.get("signal")
        if not isinstance(key, str) or not key or signal not in ("up", "down", "none"):
            continue
        if key not in last_signal:
            order.append(key)  # first-seen order, stable regardless of later flips
        last_signal[key] = signal
    vote_pos = [k for k in order if last_signal[k] == "up"]
    vote_neg = [k for k in order if last_signal[k] == "down"]

    # Weak negatives from impressions: papers SHOWN but never explicitly voted.
    # Any key that carries a vote (👍/👎, or an explicit "none" withdrawal) is a
    # JUDGED paper, not an "ignored" one, so we exclude it — `weak_negatives`
    # stays strictly distinct from the explicit vote signal. De-duped, first-seen
    # order. This is eval-only data and, like the embedding loop, is never used
    # for scoring.
    voted_keys = set(last_signal)  # every key with at least one vote event
    weak_neg: list[str] = []
    for ev in dataset.events(types=["impression"]):
        key = ev.get("canonical_key")
        if not isinstance(key, str) or not key or key in voted_keys:
            continue
        _add(weak_neg, key)

    return {
        "seed_positives": seed_pos,
        "weak_pos_terms": weak,
        "vote_positives": vote_pos,
        "vote_negatives": vote_neg,
        "weak_negatives": weak_neg,
    }


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default="data/preferences.jsonl",
                        help="preference dataset JSONL (default: data/preferences.jsonl)")
    args = parser.parse_args()

    labels = export_labels(PreferenceDataset(args.path))
    print(json.dumps(labels, ensure_ascii=False, indent=2))
    n_strong = len(labels["seed_positives"]) + len(labels["vote_positives"])
    if n_strong == 0:
        print(
            "\nNOTE: no strong paper-level positives yet (no seeds saved and no "
            "👍/👎 votes). Only weak thematic terms are available; the ranker "
            "cannot be evaluated against real labels until the feedback loop "
            "starts writing `vote` events.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
