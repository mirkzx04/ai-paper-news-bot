"""TelegramPoller — batch-process incoming commands via getUpdates.

Stateless and offset-based: each call fetches updates newer than the persisted
offset, dispatches commands, replies, and advances the offset in the Store. This
is exactly what a GitHub Actions cron tick needs (no webhook, no long-running
process) and is reused by the Phase 3 👍/👎 feedback loop.
"""

from __future__ import annotations

import logging

from src.commands.dispatch import CommandDispatcher
from src.store.base import Store
from src.telegram_api import get_updates, send_message

logger = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_offset"


class TelegramPoller:
    def __init__(self, token: str, dispatcher: CommandDispatcher, store: Store,
                 timeout: int = 20) -> None:
        self.token = token
        self.dispatcher = dispatcher
        self.store = store
        self.timeout = timeout

    def poll_once(self) -> int:
        """Fetch and process pending updates. Returns how many replies were sent."""
        raw_offset = self.store.get_meta(_OFFSET_KEY)
        offset = int(raw_offset) if raw_offset else None

        data = get_updates(self.token, offset=offset, timeout=0,
                           req_timeout=self.timeout + 5)
        if not data.get("ok"):
            logger.warning("getUpdates failed: %s", data)
            return 0

        updates = data.get("result", [])
        replies_sent = 0
        last_update_id: int | None = None
        for update in updates:
            last_update_id = update["update_id"]
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            text = message.get("text")
            if not text or not chat:
                continue
            reply = self.dispatcher.dispatch(text)
            logger.info("in: %r -> %s", text[:60], "reply" if reply else "ignored")
            if reply:
                send_message(self.token, chat["id"], reply)
                replies_sent += 1

        if last_update_id is not None:
            # Persist the NEXT offset so processed updates aren't seen again.
            self.store.set_meta(_OFFSET_KEY, str(last_update_id + 1))
        logger.info("processed %d updates, sent %d replies", len(updates), replies_sent)
        return replies_sent
