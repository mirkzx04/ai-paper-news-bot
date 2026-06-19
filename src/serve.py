"""Long-running serve mode: real-time long-poll + internal digest scheduler.

A persistent process (for a VM) that replaces the stateless 2x/day cron:
  * long-polls Telegram, so commands and 👍/👎 votes are processed in real time
    — the callback ack lands within seconds, so the toast actually shows (the
    cron's "delayed ack" limitation goes away);
  * after each poll cycle it attempts a digest, which ``app.run_digest_once``
    gates on the user's ``/set_frequency`` cadence (a cheap no-op until due).

Single-threaded by design: one loop, no shared-state locks on SQLite. SIGINT /
SIGTERM exit the loop cleanly (systemd ``Restart=always`` brings it back). The
``data/`` state lives on local disk — no gist needed in this mode.
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import replace
from datetime import datetime, timezone

from src import app
from src.config import apply_profile_overlay, load_config
from src.env import load_env
from src.flow.profile_flow import ProfileFlow
from src.report_log import ReportLog
from src.store.preference_dataset import PreferenceDataset, ProfileListener
from src.store.profile_store import ProfileStore
from src.store.sent_items_store import SentItemsStore
from src.store.sqlite_store import SqliteStore
from src.telegram_api import set_my_commands

logger = logging.getLogger("serve")

_LONG_POLL_SECONDS = 30


def _register_menu(token: str) -> None:
    """Best-effort one-time menu registration at startup (mirrors --register-menu)."""
    try:
        menu = [{"command": "creare_profile",
                 "description": "Set up your profile: read papers, authors, topics"}]
        menu += [{"command": c.name, "description": c.description[:256]}
                 for c in app.build_commands()]
        menu += [{"command": "clear", "description": "Clear recent bot messages"}]
        set_my_commands(token, menu)
    except Exception as exc:  # noqa: BLE001 - the menu is cosmetic; never block startup
        logger.warning("menu registration failed: %s", exc)


def serve_forever(config_path: str = "config/profile.yaml",
                  db_path: str = "data/bot.db",
                  overlay_path: str = "data/profile_overlay.json",
                  long_poll: int = _LONG_POLL_SECONDS,
                  max_cycles: int | None = None) -> None:
    """Run the bot as a persistent process until SIGINT/SIGTERM.

    ``max_cycles`` bounds the loop (None = forever); it exists only so tests can
    drive a finite number of cycles without signals.
    """
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise app.MissingCredentialsError(
            "--serve richiede TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID "
            "(in .env o nell'ambiente). Vedi tools/telegram_setup.py per il chat_id.")

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = SqliteStore(db_path)
    preference_dataset = PreferenceDataset()
    profile_store = ProfileStore(overlay_path, listener=ProfileListener(preference_dataset))
    sent_items = SentItemsStore(db_path)
    flow = ProfileFlow(store, profile_store)
    poller = app.build_poller(token, store, profile_store, preference_dataset,
                              admin_chat_id=chat_id, report_log=ReportLog(),
                              sent_items=sent_items, flow=flow)

    _register_menu(token)

    running = {"on": True}

    def _stop(signum, _frame):
        logger.info("signal %s received -> shutting down after this cycle", signum)
        running["on"] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info("serve up: long-poll %ss, digest cadence=%s",
                long_poll, profile_store.digest_frequency)
    cycles = 0
    try:
        while running["on"]:
            # 1) Real-time: drain commands + 👍/👎 votes (blocks up to long_poll s).
            try:
                poller.poll_once(long_poll=long_poll)
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("poll cycle failed")
            if not running["on"]:
                break
            # 2) Digest scheduler: re-read the (possibly just-edited) profile, then
            #    attempt a digest. run_digest_once skips cheaply until it's due.
            try:
                cfg = apply_profile_overlay(load_config(config_path), profile_store)
                cfg = replace(cfg, profile=app.enrich_author_ids(cfg.profile))
                app.run_digest_once(cfg, store, profile_store, preference_dataset,
                                    notifier_kind="telegram", lookback_override=None,
                                    dry_run=False, now=datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001 - already recorded+pushed by run_digest_once
                logger.exception("digest tick failed")

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
    finally:
        sent_items.close()
        store.close()
        logger.info("serve: stopped after %d cycle(s)", cycles)
