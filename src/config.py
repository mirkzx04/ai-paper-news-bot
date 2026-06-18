"""Load the YAML profile into domain objects + runtime settings."""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from src.domain.profile import ScoreWeights, UserProfile
from src.pipeline import Thresholds


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
