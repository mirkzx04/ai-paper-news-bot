"""Wider accuracy eval for the ranking — embedding vs TF-IDF vs keyword.

Builds a labelled set and measures how well each scorer ranks papers relevant to
the user's interests (MoE / interpretability / training dynamics / reasoning)
above irrelevant ones:

  positives    — curated, title-verified papers in the user's subtopics
  hard negs    — curated papers from OTHER ML subtopics (vision, RL, diffusion,
                 graphs, speech) — the meaningful, difficult discrimination
  easy negs    — recent papers sampled from non-ML arXiv categories

TF-IDF is the baseline the prior art uses (arxiv-sanity, arXiv_recbot); the point
is to see whether dense SPECTER embeddings beat lexical TF-IDF (semantic vs
lexical). All representation methods use the SAME protocol: max cosine to the
seed papers. Labels are hand-curated (not derived from the keyword list) and
arXiv results are matched by parsed id (never by return order).

Run:  .venv/bin/python tools/eval_ranking.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.domain.item import Item
from src.embedding.specter import SpecterEmbedder
from src.scoring.keyword_scorer import KeywordScorer

SEEDS = [
    ("2101.03961", "switch"),         # Switch Transformers (MoE)
    ("2209.10652", "superposition"),  # Toy Models of Superposition (interp)
    ("2201.02177", "grokking"),       # Grokking (training dynamics)
    ("2305.20050", "verify"),         # Let's Verify Step by Step (reasoning)
]

POSITIVES = [
    ("2401.04088", "mixtral"), ("2006.16668", "gshard"), ("2202.08906", "st-moe"),
    ("2112.06905", "glam"), ("2202.09368", "expert choice"), ("2401.06066", "deepseekmoe"),
    ("1701.06538", "sparsely-gated"),
    ("2309.08600", "interpretable"), ("2211.00593", "wild"), ("2209.11895", "induction heads"),
    ("2403.19647", "feature circuits"),
    ("2206.07682", "emergent"), ("2001.08361", "scaling laws"), ("2203.15556", "compute-optimal"),
    ("1912.02292", "double descent"), ("1803.03635", "lottery ticket"),
    ("2201.11903", "chain-of-thought"), ("2203.14465", "bootstrapping reasoning"),
    ("2203.11171", "self-consistency"), ("2305.10601", "tree of thoughts"),
    ("2402.03300", "deepseekmath"),
]
HARD_NEG = [
    ("2103.00020", "transferable"), ("2010.11929", "16x16"), ("2304.02643", "segment anything"),
    ("1512.03385", "residual"), ("2111.06377", "masked autoencoders"),
    ("2006.11239", "denoising diffusion"), ("2112.10752", "latent diffusion"),
    ("1406.2661", "generative adversarial"),
    ("1801.01290", "actor-critic"), ("1509.02971", "continuous control"),
    ("1707.06347", "proximal policy"), ("1312.5602", "atari"),
    ("1609.02907", "graph convolutional"), ("1710.10903", "graph attention"),
    ("2006.11477", "wav2vec"), ("2212.04356", "robust speech"),
]
EASY_NEG_CATEGORIES = ["astro-ph.GA", "q-bio.NC", "math.AG", "econ.EM"]
EASY_NEG_PER_CAT = 4
BACKGROUND_CATEGORY = "cs.LG"
BACKGROUND_N = 300

_API = "http://export.arxiv.org/api/query"


def _entry_text(entry) -> tuple[str, str, str]:
    arxiv_id = entry.id.rsplit("/abs/", 1)[-1].split("v")[0]
    title = " ".join(entry.title.split())
    abstract = " ".join(entry.get("summary", "").split())
    return arxiv_id, title, f"{title}\n\n{abstract}"


def fetch_by_ids(ids: list[str]) -> dict[str, tuple[str, str]]:
    import requests
    import feedparser
    resp = requests.get(_API, params={"id_list": ",".join(ids), "max_results": len(ids)}, timeout=60)
    resp.raise_for_status()
    out = {}
    for entry in feedparser.parse(resp.text).entries:
        aid, title, text = _entry_text(entry)
        out[aid] = (title, text)
    return out


def fetch_recent(category: str, n: int) -> list[str]:
    import requests
    import feedparser
    resp = requests.get(_API, params={
        "search_query": f"cat:{category}", "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": n}, timeout=60)
    resp.raise_for_status()
    return [_entry_text(e)[2] for e in feedparser.parse(resp.text).entries]


def roc_auc(scores, labels):
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores)); ranks[order] = np.arange(1, len(scores) + 1)
    pos = labels == 1; n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return (ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def average_precision(scores, labels):
    lab = labels[np.argsort(-scores, kind="mergesort")]
    if lab.sum() == 0:
        return float("nan")
    return float((np.cumsum(lab) / (np.arange(len(lab)) + 1) * lab).sum() / lab.sum())


def precision_at_k(scores, labels, k):
    return float(labels[np.argsort(-scores, kind="mergesort")[:k]].mean())


def ndcg_at_k(scores, labels, k):
    g = labels[np.argsort(-scores, kind="mergesort")][:k]
    d = 1.0 / np.log2(np.arange(2, len(g) + 2))
    idcg = float((np.sort(labels)[::-1][:k] * d).sum())
    return float((g * d).sum()) / idcg if idcg > 0 else float("nan")


def load_preference_positives(path: str) -> list[tuple[str, str]]:
    """Fold the user's exported seed positives into the eval as extra positives.

    Returns ``(arxiv_id, expect)`` pairs in the same shape as ``POSITIVES``.
    Since the export gives only ids (no expected title to verify against), we
    use ``""`` as the expectation, which always passes the substring title
    check in ``main`` — these ids are the user's own saved seeds, not a curated
    list we need to guard against id/title drift. Ids already in ``POSITIVES``
    are skipped to avoid duplicate candidates.
    """
    from preference_export import export_labels
    from src.store.preference_dataset import PreferenceDataset

    labels = export_labels(PreferenceDataset(path))
    have = {aid for aid, _ in POSITIVES}
    return [(aid, "") for aid in labels["seed_positives"] if aid not in have]


def main(preferences_path: str | None = None):
    extra_pos = load_preference_positives(preferences_path) if preferences_path else []
    if extra_pos:
        print(f"preference export: +{len(extra_pos)} seed positives from {preferences_path}")
    positives = POSITIVES + extra_pos

    fetched = fetch_by_ids([i for i, _ in SEEDS + positives + HARD_NEG])
    time.sleep(3)

    cands = []  # (id, label, kind, title, text)
    dropped = []
    for arxiv_id, expect in positives + HARD_NEG:
        label = 1 if (arxiv_id, expect) in positives else 0
        kind = "pos" if label else "hard"
        if arxiv_id not in fetched or expect.lower() not in fetched[arxiv_id][0].lower():
            dropped.append((arxiv_id, expect))
            continue
        title, text = fetched[arxiv_id]
        cands.append((arxiv_id, label, kind, title, text))

    for cat in EASY_NEG_CATEGORIES:
        for text in fetch_recent(cat, EASY_NEG_PER_CAT):
            cands.append((f"{cat}", 0, "easy", text.split("\n\n")[0], text))
        time.sleep(3)

    print(f"candidati usabili: {len(cands)}  (pos={sum(c[1] for c in cands)}, "
          f"hard={sum(c[2]=='hard' for c in cands)}, easy={sum(c[2]=='easy' for c in cands)})")
    if dropped:
        print(f"scartati (id/titolo non combaciano): {[d[0] for d in dropped]}")

    seed_texts = [fetched[i][1] for i, _ in SEEDS if i in fetched]
    cand_texts = [c[4] for c in cands]
    labels = np.array([c[1] for c in cands])
    kinds = np.array([c[2] for c in cands])

    # --- representations (same protocol: max cosine to seeds) ---
    embedder = SpecterEmbedder()
    S = np.asarray(embedder.encode(seed_texts), dtype=np.float32)
    C = np.asarray(embedder.encode(cand_texts), dtype=np.float32)
    emb_scores = (C @ S.T).max(axis=1)

    from sklearn.feature_extraction.text import TfidfVectorizer
    background = fetch_recent(BACKGROUND_CATEGORY, BACKGROUND_N)
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    vec.fit(background + seed_texts + cand_texts)   # transductive (fair to TF-IDF)
    seed_tfidf, cand_tfidf = vec.transform(seed_texts), vec.transform(cand_texts)
    tfidf_scores = (cand_tfidf @ seed_tfidf.T).toarray().max(axis=1)

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config", "profile.yaml"))
    kw = KeywordScorer()
    kw_scores = np.array([kw.score(
        Item(source="a", external_id=c[0], title=c[3],
             summary=c[4].split("\n\n", 1)[1] if "\n\n" in c[4] else "",
             url="", published=datetime.now(timezone.utc)), cfg.profile) for c in cands])

    methods = {"embedding (SPECTER)": emb_scores, "TF-IDF": tfidf_scores, "keyword (exact)": kw_scores}

    def subset(mask):
        return {"labels": labels[mask]}

    print(f"\n{'method':22}{'AUC':>7}{'MAP':>7}{'P@10':>7}{'nDCG@10':>9}"
          f"{'AUC vs hard':>12}{'AUC vs easy':>12}")
    hard_mask = (kinds == "pos") | (kinds == "hard")
    easy_mask = (kinds == "pos") | (kinds == "easy")
    for name, sc in methods.items():
        auc = roc_auc(sc, labels)
        auc_h = roc_auc(sc[hard_mask], labels[hard_mask])
        auc_e = roc_auc(sc[easy_mask], labels[easy_mask])
        print(f"{name:22}{auc:7.3f}{average_precision(sc, labels):7.3f}"
              f"{precision_at_k(sc, labels, 10):7.3f}{ndcg_at_k(sc, labels, 10):9.3f}"
              f"{auc_h:12.3f}{auc_e:12.3f}")

    # show embedding's worst errors (positives ranked low / hard-negs ranked high)
    print("\nembedding — errori (positivi in fondo / hard-neg in cima):")
    order = np.argsort(-emb_scores)
    ranked = [(emb_scores[i], cands[i][1], cands[i][2], cands[i][3]) for i in order]
    for s, lab, kind, title in ranked:
        if (lab == 1 and s < 0.83) or (kind == "hard" and s > 0.85):
            print(f"  {s:.3f} [{'REL' if lab else kind}] {title[:54]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ranking accuracy eval")
    parser.add_argument(
        "--preferences", nargs="?", const="data/preferences.jsonl", default=None,
        metavar="PATH",
        help="also use the user's exported seed positives from this preference "
             "dataset (default path: data/preferences.jsonl). Omit the flag to "
             "run on the hard-coded curated set only.",
    )
    args = parser.parse_args()
    main(preferences_path=args.preferences)
