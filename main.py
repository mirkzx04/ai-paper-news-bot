"""Entry point — wires the pipeline by hand (manual dependency injection).

Phase 1: arXiv -> keyword+author scoring -> console output.
  python main.py --lookback-days 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.commands.add_author import AddAuthorCommand
from src.commands.add_conference import AddConferenceCommand
from src.commands.add_keywords import AddKeywordsCommand
from src.commands.add_topic import AddTopicCommand
from src.commands.dispatch import CommandDispatcher
from src.commands.remove_author import RemoveAuthorCommand
from src.commands.remove_conference import RemoveConferenceCommand
from src.commands.remove_keywords import RemoveKeywordsCommand
from src.commands.remove_topic import RemoveTopicCommand
from src.config import apply_profile_overlay, load_config
from src.embedding.profile_vector import load_or_build
from src.embedding.specter import SpecterEmbedder
from src.enrich.semantic_scholar import resolve_author_ids
from src.env import load_env
from src.notify.base import Notifier
from src.notify.console_notifier import ConsoleNotifier
from src.notify.telegram_notifier import TelegramNotifier
from src.pipeline import Pipeline
from src.scoring.author_scorer import AuthorScorer
from src.scoring.combined import CombinedScorer
from src.scoring.embedding_scorer import EmbeddingScorer
from src.scoring.field_classifier import FieldClassifier
from src.scoring.keyword_scorer import KeywordScorer
from src.sources.arxiv_source import ArxivSource
from src.store.profile_store import ProfileStore
from src.store.sqlite_store import SqliteStore
from src.telegram_poller import TelegramPoller

logger = logging.getLogger("main")


def build_commands() -> list:
    """The Telegram slash-commands the bot understands."""
    return [
        AddAuthorCommand(),
        AddKeywordsCommand(),
        AddTopicCommand(),
        AddConferenceCommand(),
        RemoveAuthorCommand(),
        RemoveKeywordsCommand(),
        RemoveTopicCommand(),
        RemoveConferenceCommand(),
    ]


def build_notifier(kind: str, parser: argparse.ArgumentParser, field_classifier) -> Notifier:
    if kind == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            parser.error(
                "il notifier telegram richiede TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID "
                "(in .env o nell'ambiente). Vedi tools/telegram_setup.py per il chat_id."
            )
        return TelegramNotifier(token, chat_id, field_classifier=field_classifier)
    return ConsoleNotifier(field_classifier=field_classifier)


def build_pipeline(cfg, store, notifier,
                   profile_vector_path: str = "data/profile_vector.json") -> Pipeline:
    arxiv_cfg = cfg.sources.get("arxiv", {})
    sources = [
        ArxivSource(
            categories=arxiv_cfg.get("categories", ["cs.LG"]),
            max_results=int(arxiv_cfg.get("max_results", 150)),
            lookback_days=int(arxiv_cfg.get("lookback_days", 2)),
        )
    ]
    # The embedder is lazy: load_or_build only downloads/runs SPECTER when there
    # are seed papers and the cached vector is stale. With no seeds the profile
    # vector is None and EmbeddingScorer is a no-op (the model never loads).
    embedder = SpecterEmbedder()
    seed_vectors = load_or_build(list(cfg.profile.seed_arxiv_ids), embedder, profile_vector_path)
    if seed_vectors is None:
        logger.info("no seed papers -> embedding scorer is a no-op")
    scorer = CombinedScorer({
        "keyword": KeywordScorer(),
        "author": AuthorScorer(),
        "embedding": EmbeddingScorer(embedder, seed_vectors),
    })
    return Pipeline(sources, scorer, store, notifier, cfg.profile, cfg.thresholds)


def enrich_author_ids(profile, cache_path: str = "data/author_ids.json"):
    """Fill profile.author_ids from Semantic Scholar (cached), if a key is set.

    No-op without SEMANTIC_SCHOLAR_API_KEY. Resolved ids are cached by name so we
    don't re-query S2 every run; only names not yet in the cache are looked up.
    """
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if not api_key or not profile.author_names:
        return profile

    cache: dict = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}

    missing = [name for name in profile.author_names if name not in cache]
    if missing:
        cache.update(resolve_author_ids(missing, api_key=api_key))
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)

    author_ids = tuple(cache[name] for name in profile.author_names if cache.get(name))
    return replace(profile, author_ids=author_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI paper news bot (Phase 1)")
    parser.add_argument("--config", default="config/profile.yaml")
    parser.add_argument("--db", default="data/bot.db")
    parser.add_argument("--overlay", default="data/profile_overlay.json",
                        help="JSON file with the user's runtime profile additions")
    parser.add_argument("--notifier", choices=["console", "telegram"], default="console")
    parser.add_argument("--poll-commands", action="store_true",
                        help="process incoming Telegram commands (/add_author, ...) and exit")
    parser.add_argument("--lookback-days", type=int, default=None,
                        help="override the source lookback window")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't persist seen-ids (re-show items next run)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    load_env()
    cfg = load_config(args.config)
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    store = SqliteStore(args.db)
    profile_store = ProfileStore(args.overlay)

    # Command-poll mode: read pending commands, mutate the overlay, reply, exit.
    if args.poll_commands:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error("--poll-commands richiede TELEGRAM_BOT_TOKEN (in .env o nell'ambiente).")
        dispatcher = CommandDispatcher(build_commands(), profile_store)
        poller = TelegramPoller(token, dispatcher, store)
        try:
            sent = poller.poll_once()
            print(f"comandi processati, risposte inviate: {sent}")
        finally:
            store.close()
        return

    # Pipeline mode: merge the user's runtime additions on top of the YAML seed,
    # then resolve author ids via S2 (no-op without an API key).
    cfg = apply_profile_overlay(cfg, profile_store)
    cfg = replace(cfg, profile=enrich_author_ids(cfg.profile))
    field_classifier = FieldClassifier(cfg.topics)
    notifier = build_notifier(args.notifier, parser, field_classifier)
    pipeline = build_pipeline(cfg, store, notifier)

    lookback = args.lookback_days
    if lookback is None:
        lookback = int(cfg.sources.get("arxiv", {}).get("lookback_days", 2))
    since = datetime.now(timezone.utc) - timedelta(days=lookback)

    try:
        pipeline.run(since, mark_seen=not args.dry_run)
    finally:
        store.close()


if __name__ == "__main__":
    main()
