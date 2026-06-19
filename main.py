"""Entry point — wires the pipeline by hand (manual dependency injection).

Phase 1: arXiv -> keyword+author scoring -> console output.
  python main.py --lookback-days 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import traceback
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
from src.commands.report import ReportCommand
from src.config import apply_profile_overlay, load_config
from src.embedding.feedback_vectors import load_or_build_feedback_vectors
from src.embedding.profile_vector import load_or_build
from src.embedding.specter import SpecterEmbedder
from src.enrich.semantic_scholar import resolve_author_ids
from src.env import load_env
from src.error_log import ErrorLog
from src.flow.profile_flow import ProfileFlow
from src.notify.base import Notifier
from src.notify.console_notifier import ConsoleNotifier
from src.notify.telegram_notifier import TelegramNotifier
from src.pipeline import Pipeline, RunSummary
from src.report_log import ReportLog
from src.scoring.author_scorer import AuthorScorer
from src.scoring.combined import CombinedScorer
from src.scoring.embedding_scorer import EmbeddingScorer
from src.scoring.field_classifier import FieldClassifier
from src.scoring.keyword_scorer import KeywordScorer
from src.sources.arxiv_source import ArxivSource
from src.store.preference_dataset import PreferenceDataset, ProfileListener
from src.store.profile_store import ProfileStore
from src.store.sent_items_store import SentItemsStore
from src.store.sqlite_store import SqliteStore
from src.telegram_api import send_message, set_my_commands
from src.telegram_poller import TelegramPoller

logger = logging.getLogger("main")


def _admin_push(token: str, chat_id, text: str) -> None:
    """Best-effort one-off push to the bot owner (admin) in digest mode.

    Used for the failure alert and the success heartbeat. Deliberately
    exception-proof: a push is observability, not the job — a network blip,
    a 4xx, or a malformed response must never mask the pipeline's own error
    nor block resource cleanup. Any failure is logged and swallowed.
    """
    try:
        send_message(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001 - an admin push must never crash the run
        logger.warning("admin push failed: %s", exc)


def _heartbeat_text(summary: RunSummary) -> str:
    """One-line end-of-run heartbeat from a RunSummary (duck-typed on its fields).

    Lets the owner see the cron is alive, how many papers moved, and the
    scoring-error count even when those errors weren't fatal. Pure formatting,
    so it's testable without running the pipeline.
    """
    return (
        f"✅ digest: {summary.alerts} alert + {summary.digest} digest inviati"
        f" · {summary.fresh} nuovi · {summary.scoring_errors} scoring-error"
    )


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
        ReportCommand(),
    ]


def build_notifier(kind: str, parser: argparse.ArgumentParser, field_classifier,
                   sent_items=None, preference_dataset=None) -> Notifier:
    if kind == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            parser.error(
                "il notifier telegram richiede TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID "
                "(in .env o nell'ambiente). Vedi tools/telegram_setup.py per il chat_id."
            )
        return TelegramNotifier(token, chat_id, field_classifier=field_classifier,
                                sent_items=sent_items, preference_dataset=preference_dataset)
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
    seed_vectors = load_or_build(list(cfg.profile.seed_arxiv_ids), embedder, profile_vector_path,
                                 seed_texts=list(cfg.profile.seed_texts))
    if seed_vectors is None:
        logger.info("no seed papers -> embedding scorer is a no-op")
    # Feedback loop (👍/👎): votes become dynamic seeds in the embedding channel
    # — 👍 positive centers of interest, 👎 a soft margined penalty. Confined to
    # embedding (declared keyword/author signals stay intact) and anchored below
    # the onboarding seeds. With no votes this returns (None, …) and the scorer
    # behaves exactly as in Phase 2.
    fb = cfg.feedback
    pos_vecs, pos_w, neg_vecs, neg_w = load_or_build_feedback_vectors(
        PreferenceDataset(), embedder, cache_path="data/feedback_vectors.json",
        w_pos_max=fb.w_pos_max, tau_days=fb.tau_days,
        coldstart_k=fb.coldstart_k, cap_m=fb.cap_m,
    )
    scorer = CombinedScorer({
        "keyword": KeywordScorer(),
        "author": AuthorScorer(),
        "embedding": EmbeddingScorer(
            embedder, seed_vectors,
            pos_vectors=pos_vecs, pos_weights=pos_w,
            neg_vectors=neg_vecs, neg_weights=neg_w,
            baseline_neg=fb.baseline_neg, neg_lambda=fb.neg_lambda,
        ),
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
    parser.add_argument("--register-menu", action="store_true",
                        help="register the bot's slash-command menu on Telegram and exit")
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

    # Register the Telegram slash-command menu and exit.
    if args.register_menu:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error("--register-menu richiede TELEGRAM_BOT_TOKEN.")
        menu = [{"command": "creare_profile",
                 "description": "Set up your profile: read papers, authors, topics"}]
        menu += [{"command": c.name, "description": c.description[:256]} for c in build_commands()]
        # Placeholder copy (to be polished later): /clear wipes recent bot messages.
        menu += [{"command": "clear", "description": "Clear recent bot messages"}]
        resp = set_my_commands(token, menu)
        print(f"setMyCommands: {resp.status_code} {resp.text[:120]}")
        return

    cfg = load_config(args.config)
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    store = SqliteStore(args.db)
    # Every profile edit is mirrored into the append-only preference dataset
    # (data/preferences.jsonl, gist-synced). Listener is the only DI seam; with
    # it absent ProfileStore behaves exactly as before.
    preference_dataset = PreferenceDataset()
    profile_store = ProfileStore(args.overlay, listener=ProfileListener(preference_dataset))

    # Command-poll mode: read pending commands, mutate the overlay, reply, exit.
    if args.poll_commands:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error("--poll-commands richiede TELEGRAM_BOT_TOKEN (in .env o nell'ambiente).")
        admin_chat_id = os.environ.get("TELEGRAM_CHAT_ID")  # owner = admin for /reports, /errors
        sent_items = SentItemsStore(args.db)  # resolves an incoming 👍/👎 back to its paper
        dispatcher = CommandDispatcher(build_commands(), profile_store)
        flow = ProfileFlow(store, profile_store)
        poller = TelegramPoller(token, dispatcher, store, flow=flow,
                                report_log=ReportLog(), admin_chat_id=admin_chat_id,
                                preference_dataset=preference_dataset, sent_items=sent_items)
        try:
            sent = poller.poll_once()
            poller.notify_new_errors()  # end-of-run push to admin; no-op if no new errors / no admin
            print(f"comandi processati, risposte inviate: {sent}")
        finally:
            sent_items.close()
            store.close()
        return

    # Pipeline mode: merge the user's runtime additions on top of the YAML seed,
    # then resolve author ids via S2 (no-op without an API key).
    cfg = apply_profile_overlay(cfg, profile_store)
    cfg = replace(cfg, profile=enrich_author_ids(cfg.profile))
    field_classifier = FieldClassifier(cfg.topics)
    # Record each sent paper so its 👍/👎 vote (arriving on a later run) resolves
    # back to it. Only the Telegram notifier needs it; shares data/bot.db.
    sent_items = SentItemsStore(args.db) if args.notifier == "telegram" else None
    notifier = build_notifier(args.notifier, parser, field_classifier,
                              sent_items=sent_items, preference_dataset=preference_dataset)
    pipeline = build_pipeline(cfg, store, notifier)

    lookback = args.lookback_days
    if lookback is None:
        lookback = int(cfg.sources.get("arxiv", {}).get("lookback_days", 2))
    since = datetime.now(timezone.utc) - timedelta(days=lookback)

    # Observability (telegram mode only): the cron is unattended, so a crash
    # must land somewhere visible. On failure we persist the full traceback to
    # ErrorLog (so `/errors` sees it), push a concise alert to the owner, and
    # re-raise so the GitHub workflow fails (email) — the gist state step runs
    # `if: always()`. On success we push a one-line heartbeat with the counts.
    # In console mode there is no admin to push to, so behaviour is unchanged.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    is_telegram = args.notifier == "telegram"
    try:
        summary = pipeline.run(since, mark_seen=not args.dry_run)
        if is_telegram and chat_id:
            _admin_push(token, chat_id, _heartbeat_text(summary))
    except Exception as exc:
        ErrorLog().record(
            command="<digest>",
            args=f"notifier={args.notifier}",
            error=str(exc),
            traceback_str=traceback.format_exc(),
        )
        if is_telegram and chat_id:
            first_line = (str(exc) or "").splitlines()[0] if str(exc) else ""
            _admin_push(token, chat_id,
                        f"⚠️ digest run failed: {type(exc).__name__}: {first_line}")
        raise  # surface to the workflow (failed run + email); state saved by gist step
    finally:
        if sent_items is not None:
            sent_items.close()
        store.close()


if __name__ == "__main__":
    main()
