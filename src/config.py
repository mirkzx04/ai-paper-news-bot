"""Load the YAML profile into domain objects + runtime settings."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import yaml

from src.domain.profile import ScoreWeights, UserProfile
from src.pipeline import Thresholds
from src.store.profile_store import ProfileStore


@dataclass
class AppConfig:
    profile: UserProfile
    thresholds: Thresholds
    sources: dict = field(default_factory=dict)
    topics: dict = field(default_factory=dict)


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    weights_raw = raw.get("weights", {}) or {}
    weights = ScoreWeights(
        keyword=float(weights_raw.get("keyword", 0.6)),
        author=float(weights_raw.get("author", 1.0)),
        embedding=float(weights_raw.get("embedding", 0.6)),
    )
    profile = UserProfile(
        user_id=str(raw.get("user_id", "default")),
        keywords=tuple(raw.get("keywords", []) or ()),
        author_names=tuple(raw.get("authors", []) or ()),
        seed_arxiv_ids=tuple(raw.get("seed_arxiv_ids", []) or ()),
        seed_texts=tuple(raw.get("seed_texts", []) or ()),
        conferences=tuple(raw.get("conferences", []) or ()),
        weights=weights,
    )
    thr_raw = raw.get("thresholds", {}) or {}
    thresholds = Thresholds(
        digest=float(thr_raw.get("digest", 0.30)),
        alert=float(thr_raw.get("alert", 0.60)),
    )
    return AppConfig(
        profile=profile,
        thresholds=thresholds,
        sources=raw.get("sources", {}) or {},
        topics=raw.get("topics", {}) or {},
    )


def apply_profile_overlay(cfg: AppConfig, store: ProfileStore) -> AppConfig:
    """Merge the user's runtime additions (ProfileStore) on top of the YAML seed.

    The YAML stays the immutable seed; everything added via bot commands is
    unioned in here (case-insensitive dedup, seed entries first).
    """
    profile = cfg.profile
    merged_profile = replace(
        profile,
        keywords=_union(profile.keywords, store.keywords),
        author_names=_union(profile.author_names, store.authors),
        conferences=_union(profile.conferences, store.conferences),
        seed_arxiv_ids=_union(profile.seed_arxiv_ids, store.seeds),
    )

    topics = {name: list(kws) for name, kws in cfg.topics.items()}
    for name, kws in store.topics.items():
        key = next((k for k in topics if k.lower() == name.lower()), name)
        topics[key] = list(_union(tuple(topics.get(key, [])), kws))

    return replace(cfg, profile=merged_profile, topics=topics)


def _union(base: tuple[str, ...], extra: list[str]) -> tuple[str, ...]:
    seen = {x.lower() for x in base}
    out = list(base)
    for value in extra:
        if value.lower() not in seen:
            out.append(value)
            seen.add(value.lower())
    return tuple(out)
