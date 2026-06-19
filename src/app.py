"""Application wiring — component construction and the digest run logic.

Extracted from ``main.py`` so that both entry points can reuse it without
duplicating the wiring:
  - ``main.py``      — CLI, run-once (GitHub Actions cron);
  - ``src/serve.py`` — long-running loop (a VM), *future*.

This module is deliberately framework-free: nothing here imports ``argparse``
or otherwise assumes a CLI. The token-validation failure in ``build_notifier``
surfaces as a plain ``ValueError`` so each caller can translate it to its own
idiom (``main.py`` turns it back into ``parser.error(...)``). The behaviour of
the pipeline, the notifier construction, the digest cadence and the
observability pushes is byte-for-byte what ``main.py`` did before the
extraction — this is a behaviour-preserving refactor.
"""

from __future__ import annotations

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
from src.commands.set_frequency import SetFrequencyCommand
from src.embedding.feedback_vectors import load_or_build_feedback_vectors
from src.embedding.profile_vector import load_or_build
from src.embedding.specter import SpecterEmbedder
from src.enrich.semantic_scholar import resolve_author_ids
from src.error_log import ErrorLog
from src.notify.base import Notifier
from src.notify.console_notifier import ConsoleNotifier
from src.notify.telegram_notifier import TelegramNotifier
from src.pipeline import Pipeline, RunSummary
from src.scoring.author_scorer import AuthorScorer
from src.scoring.combined import CombinedScorer
from src.scoring.embedding_scorer import EmbeddingScorer
from src.scoring.field_classifier import FieldClassifier
from src.scoring.keyword_scorer import KeywordScorer
from src.sources.arxiv_source import ArxivSource
from src.store.preference_dataset import PreferenceDataset
from src.store.sent_items_store import SentItemsStore
from src.telegram_api import send_message
from src.telegram_poller import TelegramPoller

logger = logging.getLogger("app")

_LAST_DIGEST_KEY = "last_digest_at"  # meta key: UTC ISO time of the last sent digest


class MissingCredentialsError(ValueError):
    """Raised by ``build_notifier`` when the Telegram credentials are absent.

    A ``ValueError`` subtype (so the documented "raises ValueError" contract
    holds and any ``except ValueError`` keeps working), but a *distinct* type so
    a caller can tell a missing-credentials configuration error apart from a
    ``ValueError`` that bubbled up from inside ``pipeline.run``. The CLI relies on
    this to map only the former to ``parser.error`` (a usage error, exit 2) while
    letting a genuine run failure re-raise and crash as before.
    """


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
    capped = (f" (top {summary.digest} di {summary.digest_total})"
              if summary.digest_total > summary.digest else "")
    return (
        f"✅ digest: {summary.alerts} alert + {summary.digest} digest inviati{capped}"
        f" · {summary.fresh} nuovi · {summary.scoring_errors} scoring-error"
    )


def _store_db_path(store) -> str:
    """The on-disk path of the store's SQLite database (its 'main' attachment).

    The digest's ``SentItemsStore`` must point at the *same* db file as ``store``
    (votes are recovered from a table that lives there). ``main.py`` got this for
    free by passing the same ``args.db`` to both; with only ``store`` in hand here
    we recover that path from the live connection via ``PRAGMA database_list`` —
    the row named "main" carries the filename ``sqlite3.connect`` resolved. This
    is functionally identical to the old ``args.db`` (same connect target) and
    keeps ``run_digest_once``'s signature free of a redundant db-path argument.
    """
    for _, name, file in store.conn.execute("PRAGMA database_list"):
        if name == "main":
            return file
    raise RuntimeError("store has no 'main' SQLite database")  # pragma: no cover


def _parse_dt(raw):
    """Parse an ISO-8601 timestamp from meta storage; None on missing/garbage."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _digest_is_due(frequency: str, last, now) -> bool:
    """Whether to send the digest on this cron tick, given the user's frequency.

    The cron is the fixed max tick (2x/day); the user picks a frequency <= it via
    /set_frequency. Never sent yet (last is None) -> always due.
      - 2x_daily: >= ~11.5h since the last send (twice a day; the default).
      - daily:    at most once per UTC calendar day (first tick of the day sends).
      - weekly:   >= ~6.5 days since the last send (tolerates cron jitter).
    """
    if last is None:
        return True
    if frequency == "daily":
        return last.date() < now.date()
    if frequency == "weekly":
        return (now - last) >= timedelta(days=6, hours=12)
    # "2x_daily" (default) or unknown: ~twice a day, time-based so it's correct
    # both for the 2x/day cron AND the serve loop's frequent ticks (a plain
    # "always due" would fire the digest on every serve tick).
    return (now - last) >= timedelta(hours=11, minutes=30)


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
        SetFrequencyCommand(),
    ]


def build_notifier(kind: str, field_classifier, *,
                   sent_items=None, preference_dataset=None) -> Notifier:
    """Construct the notifier for ``kind`` ("telegram" or anything else -> console).

    Framework-free variant of the original ``main.build_notifier``: instead of
    calling ``parser.error(...)`` when the Telegram credentials are missing, it
    raises ``ValueError`` with the same message so the caller (CLI or serve loop)
    can translate it to its own idiom. Behaviour is otherwise identical.
    """
    if kind == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise MissingCredentialsError(
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
    return Pipeline(sources, scorer, store, notifier, cfg.profile, cfg.thresholds,
                    digest_cap=cfg.digest_cap)


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


def build_poller(token, store, profile_store, preference_dataset, *,
                 admin_chat_id, report_log, sent_items, flow) -> TelegramPoller:
    """Construct the command poller exactly as the ``--poll-commands`` branch does.

    Both entry points share this so the dispatcher/flow/feedback wiring lives in
    one place. ``flow`` is passed in (rather than built here) because the caller
    already holds the ``store``/``profile_store`` it is built from; likewise
    ``report_log``, ``sent_items`` and ``preference_dataset`` are injected so the
    caller owns their lifecycle (e.g. closing ``sent_items``).
    """
    error_log = ErrorLog()
    dispatcher = CommandDispatcher(build_commands(), profile_store, error_log=error_log)
    return TelegramPoller(token, dispatcher, store, flow=flow,
                          error_log=error_log, report_log=report_log,
                          admin_chat_id=admin_chat_id, preference_dataset=preference_dataset,
                          sent_items=sent_items)


def run_digest_once(cfg, store, profile_store, preference_dataset, *,
                    notifier_kind, lookback_override, dry_run, now) -> RunSummary | None:
    """Run a single digest tick; the shared contract for CLI and serve loop.

    Encapsulates exactly the digest branch of ``main.py``:

      1. Cadence gate. With no ``lookback_override`` (a manual override always
         sends), consult ``_digest_is_due`` against ``profile_store.digest_frequency``
         and the ``last_digest_at`` meta on ``store``. If not due, log and return
         ``None`` (the tick is skipped — no SPECTER, no notifier).
      2. Dynamic ``since``. A manual override fetches the last N days; otherwise
         everything since the last digest (or the configured arxiv lookback on the
         very first run), so nothing is missed between low-frequency sends.
      3. Build field_classifier + (telegram-only) sent_items + notifier + pipeline,
         then ``pipeline.run(since, mark_seen=not dry_run)``.
      4. On success: persist ``last_digest_at`` (unless ``dry_run``) and push the
         one-line heartbeat to the owner (telegram + chat_id present). Return the
         ``RunSummary``.
      5. On failure: record the full traceback in ``ErrorLog``, push a concise
         alert to the owner, and **re-raise** (so the cron run fails visibly).
      6. ``finally``: close ``sent_items`` only. ``store`` is the caller's — it is
         deliberately NOT closed here (the serve loop keeps it open across ticks).

    ``now`` is injected for testability; ``lookback_override`` is the optional
    ``--lookback-days`` (None = follow the cadence).
    """
    # Digest cadence: the cron is the fixed max tick (2x/day); the user's
    # /set_frequency preference decides whether THIS tick actually sends. An
    # explicit lookback override is a manual run that always sends. We check
    # before building the pipeline so a no-send tick never loads SPECTER.
    manual = lookback_override is not None
    last_dt = _parse_dt(store.get_meta(_LAST_DIGEST_KEY))
    if not manual and not _digest_is_due(profile_store.digest_frequency, last_dt, now):
        logger.info("digest skipped: frequency=%s, last=%s",
                    profile_store.digest_frequency, last_dt)
        return None

    # Dynamic lookback: fetch everything since the last digest so nothing is missed
    # between low-frequency sends (the digest is then capped to the top-N by score).
    if manual:
        since = now - timedelta(days=lookback_override)
    elif last_dt is not None:
        since = last_dt
    else:
        since = now - timedelta(days=int(cfg.sources.get("arxiv", {}).get("lookback_days", 2)))

    # Observability (telegram mode only): the cron is unattended, so a crash
    # must land somewhere visible. On failure we persist the full traceback to
    # ErrorLog (so `/errors` sees it), push a concise alert to the owner, and
    # re-raise so the GitHub workflow fails (email) — the gist state step runs
    # `if: always()`. On success we push a one-line heartbeat with the counts.
    # In console mode there is no admin to push to, so behaviour is unchanged.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    is_telegram = notifier_kind == "telegram"
    sent_items = None
    try:
        field_classifier = FieldClassifier(cfg.topics)
        # Record each sent paper so its 👍/👎 vote (arriving on a later run) resolves
        # back to it. Only the Telegram notifier needs it; shares data/bot.db.
        sent_items = SentItemsStore(_store_db_path(store)) if is_telegram else None
        notifier = build_notifier(notifier_kind, field_classifier,
                                  sent_items=sent_items, preference_dataset=preference_dataset)
        pipeline = build_pipeline(cfg, store, notifier)
        summary = pipeline.run(since, mark_seen=not dry_run)
        if not dry_run:
            store.set_meta(_LAST_DIGEST_KEY, now.isoformat())
        if is_telegram and chat_id:
            _admin_push(token, chat_id, _heartbeat_text(summary))
        return summary
    except MissingCredentialsError:
        raise
    except Exception as exc:
        ErrorLog().record(
            command="<digest>",
            args=f"notifier={notifier_kind}",
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
