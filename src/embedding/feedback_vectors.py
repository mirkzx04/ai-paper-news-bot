"""Build and cache the dynamic 👍/👎 feedback vectors for the embedding scorer.

The 👍/👎 feedback loop is confined to the *embedding* channel and is explicitly
**balanced**: 👍 papers become dynamic positive seeds (new centres of interest)
and 👎 papers a soft, margined penalty. The onboarding profile stays anchored and
dominant — these vectors carry weight ``< 1`` (vs the seeds' implicit 1.0). The
actual scoring math (max-based pos/neg terms, margin ``b_neg``, asymmetry ``λ``)
lives in :class:`~src.scoring.embedding_scorer.EmbeddingScorer`; this module only
turns the vote history into the ``(pos_vectors, pos_weights, neg_vectors,
neg_weights)`` it consumes.

Pipeline
--------
1. **Net-state per paper.** Read ``vote`` events from
   :class:`~src.store.preference_dataset.PreferenceDataset` (oldest-first) and
   keep, per ``canonical_key``, the *last* vote — a paper voted 👍 then 👎 counts
   as 👎 (and vice versa). A toggle-off (``signal=="none"``, the user withdrawing
   their vote) wins as the last write too and removes the key from both classes
   *and* from the cold-start count — as if it had never been voted. Events with
   ``text=None`` are dropped (the paper text is what we embed and it is no longer
   recoverable) but still count toward the total-vote cold-start denominator
   below, since they were real (active) feedback.
2. **Embedding (cached).** Embed each surviving paper's ``text`` as one
   L2-normalized row. The cache re-embeds only papers whose ``(key, text)`` pair
   is new or changed; unchanged papers reuse the stored row.
3. **Per-vote weight.** ``w_i = w_pos_max · decay_i · coldstart`` with
   ``decay_i = exp(-Δt_i / τ)`` (Δt_i in days), ``coldstart = min(1, N / K)``
   over the total vote count ``N``, and ``w_pos_max`` capping every feedback
   vote below the onboarding seeds. The *same* weight schema is used for 👎; the
   pos/neg asymmetry is realized by ``λ`` in the scorer, not here.
4. **Cap (ring buffer).** Keep at most ``M`` votes per class, preferring the
   highest-weight (hence most recent, since weight is monotone in recency)
   ones — bounding drift and numerosity imbalance ("contained gist").

Design note — saturation & class-balance are STRUCTURAL here
------------------------------------------------------------
The original design sketched an explicit ``1 - exp(-n/scale)`` saturation and a
pos/neg class-balancing factor. In the scorer's **max-based** (not sum-based)
framework both are largely realized *structurally*, so we deliberately do NOT
add them:

  - **Saturation.** Because the scorer takes a ``max`` over vectors (never a
    sum), N votes clustered on one topic cannot push the score past the single
    best vector's contribution. That is exactly the saturating effect an
    explicit ``1 - exp(-n/scale)`` term was meant to provide; adding it on top
    would be redundant.
  - **Class-balance.** A ``max`` is insensitive to how *many* vectors a class
    has — only to its strongest member — so a pos/neg numerosity imbalance does
    not by itself tilt the score. The per-class cap ``M`` further bounds the
    imbalance. We therefore do not normalize per class. (A leftover, opt-in
    ``class_balance`` hook is provided and documented should this assumption
    ever need revisiting, but it is OFF by default and unused by the default
    path.)

This keeps the loop a *contained* nudge rather than a clustering system.

The cache file (JSON) is keyed on the exact net-state that produced it, so a
rebuild happens only when the votes actually change — mirroring
:func:`src.embedding.profile_vector.load_or_build`.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from src.embedding.base import l2_normalize

logger = logging.getLogger(__name__)

# Defaults are the research-chosen values; main.py overrides them from
# config/profile.yaml. Kept here so the function is usable/testable standalone.
DEFAULT_TAU_DAYS = 120.0      # τ: half-ish life of a vote's influence (days)
DEFAULT_COLDSTART_K = 5       # K: votes needed for the loop to reach full weight
DEFAULT_W_POS_MAX = 0.6       # w_pos_max: cap per feedback vote (< seeds' 1.0)
DEFAULT_CAP_M = 50            # M: max votes kept per class (ring buffer)

# Returned tuple type, for readability.
FeedbackVectors = tuple[
    "np.ndarray | None", "np.ndarray | None", "np.ndarray | None", "np.ndarray | None"
]


@dataclass(frozen=True)
class _Vote:
    """One paper's resolved (net) vote, ready to be weighted/embedded."""

    canonical_key: str
    signal: str           # "up" | "down"
    text: str             # never None here (None-text votes are dropped upstream)
    ts: str               # UTC ISO-8601 timestamp of the *winning* vote


# --- net-state -------------------------------------------------------------

def _net_state(events: list[dict]) -> tuple[list[_Vote], int]:
    """Collapse the vote log to the net (last-vote-wins) state per paper.

    ``events`` is the oldest-first ``vote`` list from ``PreferenceDataset``.
    Returns ``(votes, total)`` where ``votes`` are the surviving per-paper net
    votes that are an *active* (up/down) vote with usable (non-None, non-empty)
    ``text``, and ``total`` is the number of distinct papers whose net vote is
    active up/down *regardless* of whether their text survived — this is the
    cold-start denominator ``N`` (real feedback the user gave, even on papers we
    can no longer embed).

    Toggle-off (``signal=="none"``) is honoured as a last-write-wins withdrawal:
    a ``"none"`` event overwrites any prior up/down for that key (so a 👍 later
    un-voted no longer counts), and the key is then excluded from BOTH classes
    and from ``total`` — exactly as if the user had never voted on it. Withdrawn
    keys must not nudge the ranking, nor inflate the cold-start count.

    Iterating oldest-first and overwriting per key means the last event for a
    key wins, which is the required net-state rule.
    """
    by_key: dict[str, dict] = {}
    for ev in events:
        key = ev.get("canonical_key")
        signal = ev.get("signal")
        # Accept "none" too so it can overwrite a prior up/down (last-write-wins);
        # it is dropped below. Anything else is malformed — skip it (defensive).
        if not key or signal not in ("up", "down", "none"):
            continue
        # Last write wins -> net state. Keep ts/text of the *winning* vote.
        by_key[key] = {"signal": signal, "text": ev.get("text"), "ts": ev.get("ts")}

    # Drop withdrawn keys entirely: they count neither as votes nor toward N.
    active = {k: st for k, st in by_key.items() if st["signal"] in ("up", "down")}

    total = len(active)  # distinct papers with an ACTIVE net vote -> cold-start N
    votes: list[_Vote] = []
    for key, st in active.items():
        text = st["text"]
        if not text:  # None or "" -> not embeddable; counts in `total` only
            continue
        votes.append(_Vote(canonical_key=key, signal=st["signal"], text=text, ts=st["ts"]))
    return votes, total


# --- weighting -------------------------------------------------------------

def _age_days(ts: str | None, now: datetime) -> float:
    """Δt in days between ``now`` and the ISO-8601 ``ts`` (0 if unparseable/future).

    A missing or malformed timestamp is treated as age 0 (decay 1.0): we would
    rather over- than under-weight a vote whose age we cannot read. Negative ages
    (clock skew / future ts) are clamped to 0 for the same reason.
    """
    if not ts:
        return 0.0
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = (now - parsed).total_seconds() / 86400.0
    return max(0.0, delta)


def _vote_weight(ts: str | None, now: datetime, total_votes: int,
                 w_pos_max: float, tau_days: float, coldstart_k: int) -> float:
    """``w = w_pos_max · exp(-Δt/τ) · min(1, N/K)`` for one vote."""
    decay = math.exp(-_age_days(ts, now) / tau_days)
    # min(1, N/K): with K or more total votes the loop is at full strength; with
    # fewer it is scaled down so sparse feedback barely moves the ranking.
    coldstart = min(1.0, total_votes / coldstart_k) if coldstart_k > 0 else 1.0
    return w_pos_max * decay * coldstart


# --- embedding cache -------------------------------------------------------

def _load_cache(path: str) -> dict[str, list[float]]:
    """Read the per-paper embedding cache: ``{key: {"text": str, "vec": [...]}}``.

    Returns a ``{key: row}`` map (rows as python lists) for entries we can reuse,
    indexed so a caller checks both key and text. An unreadable/old cache yields
    ``{}`` (full rebuild). Never raises.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        entries = cached["entries"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Feedback-vector cache at %s unreadable, rebuilding: %s", path, exc)
        return {}
    out: dict[str, dict] = {}
    if isinstance(entries, dict):
        for key, rec in entries.items():
            if isinstance(rec, dict) and "text" in rec and "vec" in rec:
                out[key] = rec
    return out


def _embed_with_cache(votes: list[_Vote], embedder, cache: dict[str, dict]
                      ) -> dict[str, "np.ndarray"]:
    """Embed each vote's text into one L2-normalized row, reusing the cache.

    A paper is re-embedded only when its ``(key, text)`` is new or its text
    changed since the cached row; otherwise the cached row is reused. Returns a
    ``{canonical_key: (dim,) float32 row}`` map. Rows are L2-normalized (the
    embedder normally already does, but we enforce it so dot == cosine holds).
    """
    rows: dict[str, np.ndarray] = {}
    to_embed: list[_Vote] = []
    for v in votes:
        rec = cache.get(v.canonical_key)
        if rec is not None and rec.get("text") == v.text:
            rows[v.canonical_key] = np.asarray(rec["vec"], dtype=np.float32)
        else:
            to_embed.append(v)

    if to_embed:
        texts = [v.text for v in to_embed]
        fresh = np.asarray(embedder.encode(texts), dtype=np.float32)
        fresh = l2_normalize(fresh).astype(np.float32)
        for v, row in zip(to_embed, fresh):
            rows[v.canonical_key] = row
    return rows


def _write_cache(path: str, votes: list[_Vote], rows: dict[str, "np.ndarray"]) -> None:
    """Persist ``{key: {"text", "vec"}}`` for every embedded vote. Never fatal."""
    entries = {
        v.canonical_key: {"text": v.text, "vec": rows[v.canonical_key].tolist()}
        for v in votes
        if v.canonical_key in rows
    }
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"entries": entries}, fh)
    except OSError as exc:
        logger.warning("could not write feedback-vector cache %s: %s", path, exc)


# --- assembly --------------------------------------------------------------

def _stack_class(votes: list[_Vote], rows: dict[str, "np.ndarray"],
                 now: datetime, total_votes: int, w_pos_max: float,
                 tau_days: float, coldstart_k: int, cap_m: int,
                 class_balance: float) -> tuple["np.ndarray | None", "np.ndarray | None"]:
    """Build one class's (vectors, weights), applying the per-class cap ``M``.

    Weights are ``_vote_weight`` per vote. We keep the top-``cap_m`` by weight
    (a tie broken by recency via the weight itself, which is monotone in
    recency), realizing the ring-buffer cap: the lowest-weight (oldest) votes
    are dropped first. ``class_balance`` (default 1.0) is an optional, OFF-by-
    default scalar multiplier on the whole class's weights (see module docstring
    — structurally unnecessary, exposed only as a documented hook). Returns
    ``(None, None)`` for an empty class.
    """
    if not votes:
        return None, None

    weights = np.array(
        [_vote_weight(v.ts, now, total_votes, w_pos_max, tau_days, coldstart_k) for v in votes],
        dtype=np.float32,
    )
    vectors = np.stack([rows[v.canonical_key] for v in votes]).astype(np.float32)

    if cap_m is not None and len(votes) > cap_m:
        # Keep the cap_m highest-weight votes (most recent / strongest signal).
        keep = np.argsort(weights)[::-1][:cap_m]
        keep.sort()  # preserve chronological order for readability/determinism
        vectors = vectors[keep]
        weights = weights[keep]

    if class_balance != 1.0:
        weights = weights * float(class_balance)

    return vectors, weights


def build_feedback_vectors(
    events: list[dict],
    embedder,
    *,
    now: datetime,
    cache_path: str | None = None,
    w_pos_max: float = DEFAULT_W_POS_MAX,
    tau_days: float = DEFAULT_TAU_DAYS,
    coldstart_k: int = DEFAULT_COLDSTART_K,
    cap_m: int = DEFAULT_CAP_M,
    pos_class_balance: float = 1.0,
    neg_class_balance: float = 1.0,
) -> FeedbackVectors:
    """Turn a ``vote`` event list into ``(pos_vecs, pos_w, neg_vecs, neg_w)``.

    Pure-ish and deterministic: ``now`` is injected (never ``datetime.now()``
    internally) so weights/decay are reproducible in tests. ``events`` is the
    oldest-first list from ``PreferenceDataset.events(types=["vote"])``.

    Parameters mirror the module defaults and are passed through from config in
    main.py. ``cache_path`` enables the per-paper embedding cache (re-embed only
    new/changed papers); pass ``None`` to skip caching entirely. The two
    ``*_class_balance`` args are the documented OFF-by-default hooks (see module
    docstring); leave them at 1.0 for the intended behaviour.

    Returns ``(None, None, None, None)`` when there are no usable votes. Each
    non-empty class is a ``(k, dim)`` L2-normalized float32 matrix with a
    matching ``(k,)`` float32 weight array.
    """
    votes, total_votes = _net_state(events)
    if not votes:
        return None, None, None, None

    cache = _load_cache(cache_path) if cache_path else {}
    rows = _embed_with_cache(votes, embedder, cache)
    if cache_path:
        _write_cache(cache_path, votes, rows)

    pos_votes = [v for v in votes if v.signal == "up"]
    neg_votes = [v for v in votes if v.signal == "down"]

    pos_vectors, pos_weights = _stack_class(
        pos_votes, rows, now, total_votes, w_pos_max, tau_days, coldstart_k, cap_m,
        pos_class_balance,
    )
    neg_vectors, neg_weights = _stack_class(
        neg_votes, rows, now, total_votes, w_pos_max, tau_days, coldstart_k, cap_m,
        neg_class_balance,
    )
    return pos_vectors, pos_weights, neg_vectors, neg_weights


def load_or_build_feedback_vectors(
    dataset,
    embedder,
    cache_path: str = "data/feedback_vectors.json",
    *,
    now: datetime | None = None,
    w_pos_max: float = DEFAULT_W_POS_MAX,
    tau_days: float = DEFAULT_TAU_DAYS,
    coldstart_k: int = DEFAULT_COLDSTART_K,
    cap_m: int = DEFAULT_CAP_M,
    pos_class_balance: float = 1.0,
    neg_class_balance: float = 1.0,
) -> FeedbackVectors:
    """Convenience wrapper: read votes from a ``PreferenceDataset`` and build.

    ``dataset`` may be a :class:`~src.store.preference_dataset.PreferenceDataset`
    (anything with ``.events(types=...)``) or a path string to the JSONL log.
    ``now`` defaults to ``datetime.now(timezone.utc)`` *here* (the impure edge),
    keeping the underlying :func:`build_feedback_vectors` deterministic.

    Note: the weights depend on ``now`` (time decay), so unlike the seed-vector
    cache the *embeddings* are cached but the weights are recomputed every call.
    """
    if isinstance(dataset, (str, os.PathLike)):
        from src.store.preference_dataset import PreferenceDataset

        dataset = PreferenceDataset(str(dataset))
    if now is None:
        now = datetime.now(timezone.utc)

    events = dataset.events(types=["vote"])
    return build_feedback_vectors(
        events,
        embedder,
        now=now,
        cache_path=cache_path,
        w_pos_max=w_pos_max,
        tau_days=tau_days,
        coldstart_k=coldstart_k,
        cap_m=cap_m,
        pos_class_balance=pos_class_balance,
        neg_class_balance=neg_class_balance,
    )
