"""Entry point — wires the pipeline by hand (manual dependency injection).

Phase 1: arXiv -> keyword+author scoring -> console output.
  python main.py --lookback-days 2
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone

from src import app
from src.app import build_commands, build_notifier, build_pipeline, enrich_author_ids
from src.config import apply_profile_overlay, load_config
from src.env import load_env
from src.flow.profile_flow import ProfileFlow
from src.report_log import ReportLog
from src.store.preference_dataset import PreferenceDataset, ProfileListener
from src.store.profile_store import ProfileStore, UserProfileStoreProvider
from src.store.sent_items_store import SentItemsStore
from src.store.sqlite_store import SqliteStore
from src.telegram_api import set_my_commands

logger = logging.getLogger("main")


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
    parser.add_argument("--serve", action="store_true",
                        help="run as a long-running process: real-time long-poll + digest scheduler")
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

    # Long-running serve mode (for a VM): real-time command/vote long-polling +
    # an internal digest scheduler honoring /set_frequency. Runs until SIGINT/SIGTERM.
    if args.serve:
        from src.serve import serve_forever
        try:
            serve_forever(config_path=args.config, db_path=args.db, overlay_path=args.overlay)
        except app.MissingCredentialsError as exc:
            parser.error(str(exc))
        return

    cfg = load_config(args.config)
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    store = SqliteStore(args.db)
    # Every profile edit is mirrored into the append-only preference dataset
    # (data/preferences.jsonl, gist-synced). Listener is the only DI seam; with
    # it absent ProfileStore behaves exactly as before.
    preference_dataset = PreferenceDataset()
    profile_store = ProfileStore(args.overlay, listener=ProfileListener(preference_dataset))
    user_profiles = UserProfileStoreProvider(
        args.overlay,
        listener_factory=lambda user_id: ProfileListener(preference_dataset, user_id=user_id),
    )

    # Command-poll mode: read pending commands, mutate the overlay, reply, exit.
    if args.poll_commands:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error("--poll-commands richiede TELEGRAM_BOT_TOKEN (in .env o nell'ambiente).")
        admin_chat_id = os.environ.get("TELEGRAM_CHAT_ID")  # owner = admin for /reports, /errors
        sent_items = SentItemsStore(args.db)  # resolves an incoming 👍/👎 back to its paper
        flow = ProfileFlow(store, profile_store)
        poller = app.build_poller(token, store, profile_store, preference_dataset,
                                  admin_chat_id=admin_chat_id, report_log=ReportLog(),
                                  sent_items=sent_items, flow=flow,
                                  profile_store_provider=user_profiles)
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

    # The whole digest tick (cadence gate, dynamic lookback, pipeline run, and the
    # owner heartbeat/error pushes) lives in app.run_digest_once so a future
    # long-running serve loop reuses it verbatim. main owns the store's lifecycle:
    # run_digest_once never closes it (the serve loop reuses it across ticks), so
    # we close it here on every path — skip (None), success, or re-raised failure.
    now = datetime.now(timezone.utc)
    try:
        app.run_digest_once(
            cfg, store, profile_store, preference_dataset,
            notifier_kind=args.notifier, lookback_override=args.lookback_days,
            dry_run=args.dry_run, now=now,
        )
    except app.MissingCredentialsError as exc:
        # build_notifier raises this (a ValueError subtype, no argparse dependency)
        # when the Telegram credentials are missing; translate it back to the CLI
        # idiom so the user-facing experience (exit 2 + usage) is identical to
        # before. A plain ValueError surfacing from pipeline.run is deliberately
        # NOT caught here — it re-raises and crashes with a traceback, as before.
        parser.error(str(exc))
    finally:
        store.close()


if __name__ == "__main__":
    main()
