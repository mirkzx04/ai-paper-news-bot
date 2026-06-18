"""TelegramPoller — batch-process incoming commands via getUpdates.

Stateless and offset-based: each call fetches updates newer than the persisted
offset, dispatches commands, replies, and advances the offset in the Store. This
is exactly what a GitHub Actions cron tick needs (no webhook, no long-running
process) and is reused by the Phase 3 👍/👎 feedback loop.
"""

from __future__ import annotations

import logging
import traceback

from src.commands.dispatch import CommandDispatcher
from src.error_log import ErrorLog
from src.store.base import Store
from src.telegram_api import delete_message, get_updates, send_message

logger = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_offset"
_CLEAR_COMMAND = "clear"
# How many message-ids to walk backward from the /clear trigger (inclusive).
_CLEAR_WINDOW = 50


class TelegramPoller:
    def __init__(self, token: str, dispatcher: CommandDispatcher, store: Store,
                 flow=None, error_log: ErrorLog | None = None, timeout: int = 20) -> None:
        self.token = token
        self.dispatcher = dispatcher
        self.store = store
        self.flow = flow  # optional ProfileFlow: handles /creare_profile + active flows first
        self.error_log = error_log or ErrorLog()
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
            # Whole-message handling is guarded so one bad message (a flow/clear
            # bug) can't crash the poll. The dispatcher already catches command
            # errors itself; this is the net for everything else.
            reply = None
            try:
                # /clear is handled before flow/dispatcher: it has no reply (the
                # deletions are the effect) and must NOT reach the dispatcher,
                # which would otherwise answer "unknown command".
                if self._is_clear_command(text):
                    self._clear_recent(chat["id"], message.get("message_id"))
                    continue
                # The profile-creation flow gets first refusal (it owns
                # /creare_profile and any mid-onboarding chat); else command dispatch.
                if self.flow is not None:
                    reply = self.flow.maybe_handle(chat["id"], text)
                if reply is None:
                    reply = self.dispatcher.dispatch(text)
            except Exception as exc:  # never let one bad message kill the poll
                self.error_log.record(command="<message>", args=text[:200],
                                      error=repr(exc), traceback_str=traceback.format_exc())
                reply = "Command execution failed"
            logger.info("in: %r -> %s", text[:60], "reply" if reply else "ignored")
            if reply:
                send_message(self.token, chat["id"], reply)
                replies_sent += 1

        if last_update_id is not None:
            # Persist the NEXT offset so processed updates aren't seen again.
            self.store.set_meta(_OFFSET_KEY, str(last_update_id + 1))
        logger.info("processed %d updates, sent %d replies", len(updates), replies_sent)
        return replies_sent

    @staticmethod
    def _is_clear_command(text: str) -> bool:
        """True if `text` is exactly /clear (optionally with a @botname suffix).

        Mirrors the dispatcher's parsing: strip the leading slash, take the first
        token, drop a trailing "@botname", compare case-insensitively. Anything
        with extra arguments after the command is not treated as /clear.
        """
        text = (text or "").strip()
        if not text.startswith("/"):
            return False
        head, _, _ = text[1:].partition(" ")
        name = head.split("@", 1)[0].lower()  # strip "@botname" suffix
        return name == _CLEAR_COMMAND

    def _clear_recent(self, chat_id, trigger_message_id: int | None) -> None:
        """Best-effort delete of the /clear message and the messages before it.

        Walks message-ids backward from the trigger and calls deleteMessage on
        each. The bot can only delete its own messages (and only within 48h), so
        the user's messages just fail silently — we ignore every result.
        """
        if trigger_message_id is None:
            return
        for candidate in range(trigger_message_id, trigger_message_id - _CLEAR_WINDOW, -1):
            if candidate > 0:
                delete_message(self.token, chat_id, candidate)
        logger.info("cleared chat %s", chat_id)
