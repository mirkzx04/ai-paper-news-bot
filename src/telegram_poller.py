"""TelegramPoller — batch-process incoming commands via getUpdates.

Stateless and offset-based: each call fetches updates newer than the persisted
offset, dispatches commands, replies, and advances the offset in the Store. This
is exactly what a GitHub Actions cron tick needs (no webhook, no long-running
process) and is reused by the Phase 3 👍/👎 feedback loop.
"""

from __future__ import annotations

import html
import logging
import traceback

import requests

from src.commands.dispatch import CommandDispatcher
from src.error_log import ErrorLog
from src.report_log import ReportLog
from src.store.base import Store
from src.telegram_api import (
    answer_callback_query,
    delete_message,
    edit_message_reply_markup,
    get_updates,
    send_message,
)
from src.user_identity import telegram_user_id

logger = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_offset"
_CLEAR_COMMAND = "clear"

# Inline 👍/👎 feedback callback_data: "fb:<u|d>:<token>" (see TelegramNotifier).
# The token is the paper's canonical key, or a short hash when the key would
# overflow Telegram's 64-byte callback_data budget.
_FEEDBACK_PREFIX = "fb:"
# Map the compact signal letter in the callback_data to the schema's signal word.
# There is no letter for "none": a toggle-off is derived (re-tapping the letter
# of the CURRENT vote), so the callback_data the buttons carry stays "u"/"d".
_FEEDBACK_SIGNALS = {"u": "up", "d": "down"}
# Toasts shown to the user after a vote (answerCallbackQuery `text`). "none" is
# the toggle-off (re-tapping your own vote withdraws it).
_FEEDBACK_TOASTS = {"up": "👍 registrato", "down": "👎 registrato",
                    "none": "↩️ voto rimosso"}

# Affordance: after a vote we re-render the inline keyboard so the chosen option
# is visibly marked (a ✅ before it) and the other stays neutral; a toggle-off
# restores both to neutral. The callback_data is rebuilt from the token so the
# user can keep (re-)voting — only the button *text* changes.
_FEEDBACK_BTN_NEUTRAL = {"up": "👍", "down": "👎"}
_FEEDBACK_BTN_CHOSEN = {"up": "✅ 👍", "down": "✅ 👎"}
# How many message-ids to walk backward from the /clear trigger (inclusive).
_CLEAR_WINDOW = 50

# Inverse of _FEEDBACK_SIGNALS: the compact callback_data letter per signal word.
# Used to rebuild a paper's 👍/👎 callback_data when re-rendering its keyboard.
_FEEDBACK_LETTERS = {"up": "u", "down": "d"}


def _feedback_markup(token: str, signal: str) -> dict:
    """Build the 👍/👎 inline keyboard for a paper in net state `signal`.

    `signal` is the paper's *current* net vote: ``"up"``/``"down"`` mark the
    chosen button with a ✅ and leave the other neutral; anything else (notably
    ``"none"`` after a toggle-off) yields the fully neutral row. The
    callback_data is always ``"fb:u:<token>"`` / ``"fb:d:<token>"`` — identical
    to what :class:`~src.notify.telegram_notifier.TelegramNotifier` emits at send
    time — so the buttons keep working for a (re-)vote. This is the single source
    of truth for re-rendering the keyboard after a vote (the affordance).
    """
    def _label(sig: str) -> str:
        return _FEEDBACK_BTN_CHOSEN[sig] if sig == signal else _FEEDBACK_BTN_NEUTRAL[sig]

    return {"inline_keyboard": [[
        {"text": _label("up"), "callback_data": f"{_FEEDBACK_PREFIX}{_FEEDBACK_LETTERS['up']}:{token}"},
        {"text": _label("down"), "callback_data": f"{_FEEDBACK_PREFIX}{_FEEDBACK_LETTERS['down']}:{token}"},
    ]]}

# Owner-only inspection commands (handled in the poller, like /clear, because
# they need the chat id to verify the sender is the admin).
_REPORTS_COMMAND = "reports"
_ERRORS_COMMAND = "errors"
# Default number of records shown by /reports and /errors with no argument.
_ADMIN_DEFAULT_N = 10
# Hard cap on how many records a single /reports|/errors call will render, so a
# huge N can't build a multi-thousand-line message that Telegram would reject.
_ADMIN_MAX_N = 50
# Per-record text budgets (Telegram caps a message at 4096 chars; we stay well
# under by trimming each record). Report bodies and tracebacks are the only
# unbounded fields.
_REPORT_BODY_MAX_CHARS = 600
_ERROR_TRACEBACK_MAX_CHARS = 500
_ERROR_TEXT_MAX_CHARS = 500
_ADMIN_MESSAGE_BUDGET = 3900
# How many of the newest errors to inline in the end-of-run push notification.
_PUSH_ERROR_PREVIEW = 2


class TelegramPoller:
    def __init__(self, token: str, dispatcher: CommandDispatcher, store: Store,
                 flow=None, error_log: ErrorLog | None = None,
                 report_log: ReportLog | None = None, admin_chat_id: str | None = None,
                 preference_dataset=None, sent_items=None, profile_store_provider=None,
                 user_id_resolver=telegram_user_id,
                 timeout: int = 20) -> None:
        self.token = token
        self.dispatcher = dispatcher
        self.store = store
        self.flow = flow  # optional ProfileFlow: handles /creare_profile + active flows first
        self.error_log = error_log or ErrorLog()
        self.report_log = report_log or ReportLog()
        # 👍/👎 feedback loop (both optional; both default None => feature OFF and
        # the poller behaves exactly as before). `preference_dataset` is the
        # append-only PreferenceDataset votes are written into; `sent_items` is
        # the SentItemsStore used to recover the voted paper's text/score/
        # breakdown (the callback_data alone can't carry them). With
        # preference_dataset None, callback_query updates are simply ignored.
        self.preference_dataset = preference_dataset
        self.sent_items = sent_items
        self.profile_store_provider = profile_store_provider
        self.user_id_resolver = user_id_resolver
        # str() the chat id so a numeric env value and Telegram's int id compare
        # equal; None disables the owner-only commands (they fall through to the
        # dispatcher, which answers "unknown command" — no leak that they exist).
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id is not None else None
        self.timeout = timeout
        # Baseline error count, captured at construction, so new_errors_this_run()
        # can report only the errors recorded by THIS process (CI runs the poller
        # once per cron tick). Snapshot now, before any poll has logged anything.
        self._errors_at_start = self.error_log.count()

    def _user_id_from_sender(self, sender: dict | None) -> str | None:
        try:
            return self.user_id_resolver(sender)
        except Exception as exc:  # noqa: BLE001 - identity failure must not break polling
            logger.warning("anonymous user id resolution failed: %s", exc)
            return None

    def _profile_store_for(self, user_id: str | None):
        if self.profile_store_provider is None or user_id is None:
            return None
        try:
            return self.profile_store_provider.for_user(user_id)
        except Exception as exc:  # noqa: BLE001 - fall back to dispatcher default
            logger.warning("profile store resolution failed for %s: %s", user_id, exc)
            return None

    def poll_once(self, long_poll: int = 0) -> int:
        """Fetch and process pending updates. Returns how many replies were sent.

        `long_poll` (seconds) selects the getUpdates mode. With the default
        ``0`` the call returns immediately (run-once, GitHub-Actions behaviour —
        unchanged). With ``long_poll > 0`` it switches to Telegram long-polling:
        getUpdates blocks server-side for up to `long_poll` seconds waiting for
        an update, and the HTTP read timeout is set to ``long_poll + 5`` so
        `requests` outlives the server hold. Everything after the fetch
        (command/callback/flow dispatch, offset advance, return value) is
        identical in both modes.

        Defensive: a network/HTTP error from getUpdates (e.g. the long-poll read
        timing out, or a connection drop) is swallowed and reported as 0 updates
        processed, so a transient failure can never break the long-running serve
        loop — the next tick simply retries from the same offset.
        """
        raw_offset = self.store.get_meta(_OFFSET_KEY)
        offset = int(raw_offset) if raw_offset else None

        # timeout=0 => immediate return (run-once); timeout=long_poll => block up
        # to long_poll s. req_timeout always outlives the server hold (get_updates
        # also clamps this, but we set it explicitly to keep both paths obvious).
        req_timeout = (long_poll + 5) if long_poll > 0 else (self.timeout + 5)
        try:
            data = get_updates(self.token, offset=offset, timeout=long_poll,
                               req_timeout=req_timeout)
        except requests.RequestException as exc:
            # A long-poll naturally times out with no updates; treat any transport
            # error as "nothing to process" so the serve loop keeps going.
            logger.warning("getUpdates request error: %s", exc)
            return 0
        if not data.get("ok"):
            logger.warning("getUpdates failed: %s", data)
            return 0

        updates = data.get("result", [])
        replies_sent = 0
        last_update_id: int | None = None
        for update in updates:
            last_update_id = update["update_id"]
            # 👍/👎 inline-button taps arrive as callback_query updates (not
            # messages). Handle them first and skip the message path. Fully
            # guarded: a feedback bug must never crash the poll. When the feature
            # is off (no preference_dataset) the callback is acknowledged so the
            # client's spinner stops, but nothing is logged.
            callback = update.get("callback_query")
            if callback is not None:
                try:
                    self._handle_callback_query(callback)
                except Exception as exc:  # never let one bad callback kill the poll
                    self.error_log.record(command="<callback>",
                                          args=str(callback.get("data"))[:200],
                                          error=repr(exc), traceback_str=traceback.format_exc())
                continue
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            user_id = self._user_id_from_sender(message.get("from"))
            active_profile_store = self._profile_store_for(user_id)
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
                # Owner-only inspection commands. Intercepted here (before flow /
                # dispatcher) ONLY for the admin chat, because they need the chat
                # id to authorize. For anyone else we don't intercept: the message
                # falls through to the dispatcher, which replies "unknown command"
                # — so a non-admin can't even tell these commands exist.
                if self._is_admin(chat["id"]):
                    admin_reply = self._handle_admin_command(text)
                    if admin_reply is not None:
                        send_message(self.token, chat["id"], admin_reply, parse_mode="HTML")
                        replies_sent += 1
                        continue
                # The profile-creation flow gets first refusal (it owns
                # /creare_profile and any mid-onboarding chat); else command dispatch.
                if self.flow is not None:
                    reply = self.flow.maybe_handle(
                        chat["id"], text, profile_store=active_profile_store, scope_id=user_id)
                if reply is None:
                    if active_profile_store is None:
                        reply = self.dispatcher.dispatch(text)
                    else:
                        reply = self.dispatcher.dispatch(text, store=active_profile_store)
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

    # -------------------------------------------------------------- feedback --
    @staticmethod
    def _parse_feedback_data(data) -> tuple[str, str] | None:
        """Parse inline-button callback_data into ``(signal, token)``.

        Accepts ``"fb:u:<token>"`` / ``"fb:d:<token>"`` and returns
        ``("up"|"down", token)``. Returns ``None`` for anything that isn't a
        well-formed feedback payload (wrong prefix, unknown signal letter, empty
        token, non-string) so the caller can ignore garbage without raising. The
        token may itself contain ':' (canonical keys like ``arxiv:2401.1`` do),
        so we split only on the first two colons.
        """
        if not isinstance(data, str) or not data.startswith(_FEEDBACK_PREFIX):
            return None
        parts = data.split(":", 2)  # ["fb", "<u|d>", "<token possibly with colons>"]
        if len(parts) != 3:
            return None
        _, letter, token = parts
        signal = _FEEDBACK_SIGNALS.get(letter)
        if signal is None or not token:
            return None
        return signal, token

    def _handle_callback_query(self, callback: dict) -> None:
        """Turn one 👍/👎 callback_query into a ``vote`` event, then ack it.

        Steps: parse the tapped signal + token, resolve the paper from
        `sent_items` (text/score/breakdown), append a ``vote`` event to the
        preference dataset (toggle-off when the tapped signal repeats the current
        vote — see ``_log_vote``), re-render the inline keyboard to mark the new
        net state (best-effort affordance), and ALWAYS answerCallbackQuery to
        stop the client spinner. Defensive throughout: the ack is sent in a
        `finally` so even an unexpected error still clears the spinner, and the
        keyboard edit is itself best-effort (a failed cosmetic edit never breaks
        the poll, and the vote is already persisted by the time we attempt it).
        """
        parsed = self._parse_feedback_data(callback.get("data"))
        user_id = self._user_id_from_sender(callback.get("from"))
        toast: str | None = None
        try:
            # Feature off, or unparseable data: nothing to log. Still ack below.
            if self.preference_dataset is None or parsed is None:
                return
            tapped_signal, token = parsed
            canonical_key = token
            text = score = breakdown = None
            # Recover what the paper was shown with. If the row was pruned (vote
            # arrived too late) we still log the signal — never lose it — with
            # text/score/breakdown None and the token as the canonical key.
            if self.sent_items is not None:
                record = self.sent_items.get(token)
                if record is not None:
                    canonical_key = record.get("canonical_key") or token
                    text = record.get("text")
                    score = record.get("score")
                    breakdown = record.get("breakdown")
            # Re-tapping your own current vote withdraws it (toggle-off ->
            # "none"); otherwise the tapped signal becomes the net vote.
            new_signal = self._effective_signal(tapped_signal, canonical_key, user_id=user_id)
            self._log_vote(new_signal, canonical_key, text, score, breakdown, user_id=user_id)
            toast = _FEEDBACK_TOASTS.get(new_signal)
            # Affordance: mark the new net state on the buttons (best-effort).
            self._refresh_feedback_markup(callback, token, new_signal)
        finally:
            cq_id = callback.get("id")
            if cq_id is not None:
                # answerCallbackQuery is defensive (never raises); the spinner must
                # always be stopped, even if logging above failed.
                answer_callback_query(self.token, cq_id, text=toast)

    def _effective_signal(self, tapped_signal: str, canonical_key: str,
                          user_id: str | None = None) -> str:
        """Resolve the tapped emoji into the new net signal, honouring toggle-off.

        Re-tapping the emoji of the *current* vote withdraws it: we return
        ``"none"`` (a documented ``vote`` signal meaning "no preference"). Tapping
        the other emoji — or voting a paper with no current vote — returns the
        tapped signal unchanged. This is a deliberate behaviour change from the
        MVP, where a re-tap was a silent no-op (the owner wants an explicit
        un-vote so a mis-tap is recoverable from the chat itself).
        """
        return "none" if self._current_vote_signal(canonical_key, user_id=user_id) == tapped_signal else tapped_signal

    def _refresh_feedback_markup(self, callback: dict, token: str, signal: str) -> None:
        """Re-render the inline keyboard to reflect `signal`, if it changed.

        Rebuilds the 👍/👎 row with the chosen option marked (✅) and the rest
        neutral — ``signal=="none"`` yields the fully neutral row. The
        callback_data is rebuilt from `token` so the buttons keep working for a
        re-vote. We skip the API call when the new keyboard equals the one
        already on the message (avoids a useless editMessageReplyMarkup, which
        Telegram would reject as "message is not modified"). Fully best-effort:
        a missing chat/message id, or a failed edit, is silently ignored.
        """
        try:
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            message_id = message.get("message_id")
            if chat_id is None or message_id is None:
                return
            new_markup = _feedback_markup(token, signal)
            # Skip a no-op edit: if the message already carries this exact keyboard
            # there's nothing to change (and Telegram errors on an identical edit).
            current = message.get("reply_markup")
            if current == new_markup:
                return
            edit_message_reply_markup(self.token, chat_id, message_id, new_markup)
        except Exception as exc:  # noqa: BLE001 — a cosmetic edit must never break polling
            # The vote is already persisted; the keyboard refresh is pure UX, so
            # swallow any failure here (don't even surface it to the error log).
            logger.warning("feedback keyboard refresh failed for %s: %s", token, exc)

    def _log_vote(self, signal: str, canonical_key: str, text, score, breakdown,
                  user_id: str | None = None) -> None:
        """Append a ``vote`` event for the resolved net `signal`, unless redundant.

        `signal` is the *new net state* already resolved by ``_effective_signal``:
        ``"up"``/``"down"`` for a vote, or ``"none"`` for a toggle-off (the user
        re-tapped their own current vote to withdraw it). The current vote for a
        key is the most recent ``vote`` event for it; we skip the append only
        when `signal` already equals it (defensive guard against a redundant
        write — e.g. two identical updates in one batch). Events stay append-only
        — we never rewrite history, so a toggle-off is recorded as a fresh
        ``signal:"none"`` event, not a deletion.

        Schema written (matches PreferenceDataset's documented ``vote`` event):
            {"type": "vote", "user_id": str, "signal": "up"|"down"|"none",
             "canonical_key": str, "score": float|None, "breakdown": dict|None,
             "text": str|None}
        The ``"none"`` signal is consumed downstream as "no preference": the
        embedding feedback loop excludes such keys from both classes, and the
        label export drops them from positives/negatives (net-state coherence).
        """
        if self._current_vote_signal(canonical_key, user_id=user_id) == signal:
            logger.info("vote %s on %s matches current net state; not re-appending",
                        signal, canonical_key)
            return
        event = {
            "type": "vote",
            "signal": signal,
            "canonical_key": canonical_key,
            "score": score,
            "breakdown": breakdown,
            "text": text,
        }
        if user_id is not None:
            event["user_id"] = user_id
        self.preference_dataset.log(event)
        logger.info("logged vote %s on %s", signal, canonical_key)

    def _current_vote_signal(self, canonical_key: str, user_id: str | None = None) -> str | None:
        """The net vote for `canonical_key`: the most recent ``vote`` signal, or None.

        Returns ``"up"``/``"down"`` for an active vote, ``"none"`` when the last
        action was a toggle-off (withdrawn vote), or ``None`` when the paper has
        never been voted on (also on any read error — PreferenceDataset.events is
        itself defensive). Note ``"none"`` and ``None`` are distinct: ``None`` is
        "never voted" (a first tap on either emoji registers a vote), while
        ``"none"`` is "explicitly withdrawn" (a tap on either emoji registers a
        fresh vote — neither emoji equals "none", so ``_effective_signal`` never
        toggles off from this state).
        """
        latest: str | None = None
        for ev in self.preference_dataset.events(types=["vote"], user_id=user_id):
            if ev.get("canonical_key") == canonical_key:
                latest = ev.get("signal")
        return latest

    # ----------------------------------------------------------------- admin --
    def _is_admin(self, chat_id) -> bool:
        """True if `chat_id` is the configured owner chat. False if unset."""
        return self.admin_chat_id is not None and str(chat_id) == self.admin_chat_id

    def _handle_admin_command(self, text: str) -> str | None:
        """Route an owner-only command to its handler; None if `text` isn't one.

        Parsing mirrors the dispatcher: strip the slash, take the first token,
        drop a trailing "@botname", compare case-insensitively. The remainder is
        the (optional) argument string, where we look for the record count N.
        """
        text = (text or "").strip()
        if not text.startswith("/"):
            return None
        head, _, args = text[1:].partition(" ")
        name = head.split("@", 1)[0].lower()  # strip "@botname" suffix
        if name == _REPORTS_COMMAND:
            return self._format_reports(self._parse_admin_n(args))
        if name == _ERRORS_COMMAND:
            return self._format_errors(self._parse_admin_n(args))
        return None

    @staticmethod
    def _parse_admin_n(args: str) -> int:
        """Parse the optional count argument of /reports|/errors.

        Empty / missing / non-integer -> the default. A valid integer is clamped
        to [1, _ADMIN_MAX_N] so a huge or non-positive N can't break the reply.
        """
        token = (args or "").strip().split(" ", 1)[0] if (args or "").strip() else ""
        try:
            n = int(token)
        except ValueError:
            return _ADMIN_DEFAULT_N
        if n < 1:
            return 1
        return min(n, _ADMIN_MAX_N)

    def _format_reports(self, n: int) -> str:
        """Render the newest `n` reports as one HTML message (newest last)."""
        records = self.report_log.recent(n)
        if not records:
            return "📋 No reports yet."
        total = self.report_log.count()
        lines = [f"📋 <b>Reports</b> — showing {len(records)} of {total}"]
        for rec in records:
            ts = html.escape(str(rec.get("timestamp", "?")))
            body = str(rec.get("report", ""))
            if len(body) > _REPORT_BODY_MAX_CHARS:
                body = body[:_REPORT_BODY_MAX_CHARS].rstrip() + "…"
            lines.append(f"\n🕒 <i>{ts}</i>\n{html.escape(body)}")
        return "\n".join(lines)

    def _format_errors(self, n: int) -> str:
        """Render the newest `n` errors as one HTML message (newest last).

        Each record shows timestamp, command, and error; the traceback is tail-
        truncated to the last `_ERROR_TRACEBACK_MAX_CHARS` chars (the bottom of a
        traceback — the actual exception — is the useful part). The full reply is
        also capped below Telegram's 4096-char limit; if many records are huge, we
        keep the newest records and omit older ones.
        """
        records = self.error_log.recent(n)
        if not records:
            return "✅ No errors logged."
        total = self.error_log.count()
        header = f"⚠️ <b>Errors</b> — showing {{shown}} of {total}"
        blocks: list[str] = []
        used = len(header.format(shown=len(records)))
        omitted = 0
        for rec in reversed(records):
            ts = html.escape(str(rec.get("timestamp", "?")))
            cmd = html.escape(str(rec.get("command", "?")))
            err_raw = str(rec.get("error", ""))
            if len(err_raw) > _ERROR_TEXT_MAX_CHARS:
                err_raw = err_raw[:_ERROR_TEXT_MAX_CHARS].rstrip() + "…"
            err = html.escape(err_raw)
            block = [f"\n🕒 <i>{ts}</i>  <code>{cmd}</code>", err]
            tb = rec.get("traceback")
            if tb:
                tb = str(tb)
                if len(tb) > _ERROR_TRACEBACK_MAX_CHARS:
                    # Keep the TAIL: the last frames + the exception line.
                    tb = "…" + tb[-_ERROR_TRACEBACK_MAX_CHARS:]
                block.append(f"<pre>{html.escape(tb)}</pre>")
            rendered = "\n".join(block)
            # +1 for the newline join; leave a little room for an omission note.
            if used + len(rendered) + 80 > _ADMIN_MESSAGE_BUDGET:
                omitted += 1
                continue
            blocks.append(rendered)
            used += len(rendered) + 1
        blocks.reverse()
        shown = len(blocks)
        lines = [header.format(shown=shown)]
        lines.extend(blocks)
        if omitted:
            lines.append(f"\n…{omitted} older error(s) omitted to fit Telegram limits.")
        return "\n".join(lines)

    # ----------------------------------------------------- end-of-run notify --
    def new_errors_this_run(self) -> int:
        """How many errors were recorded since this poller was constructed.

        Compares the current on-disk error count against the baseline snapshot
        taken in __init__. Non-negative; 0 when nothing new was logged. NEVER
        raises (count() is itself defensive).
        """
        return max(0, self.error_log.count() - self._errors_at_start)

    def summarize_new_errors(self) -> str | None:
        """Build the end-of-run admin push, or None if there's nothing to send.

        Returns a short HTML summary ("⚠️ N error(s) this run" + the newest
        `_PUSH_ERROR_PREVIEW` records) when new errors were logged during this
        process, else None. The caller decides whether/where to send it (so the
        poller stays free of a hard dependency on the admin chat for pushes).
        """
        new = self.new_errors_this_run()
        if new <= 0:
            return None
        plural = "s" if new != 1 else ""
        lines = [f"⚠️ <b>{new} error{plural} this run</b>"]
        for rec in self.error_log.recent(min(new, _PUSH_ERROR_PREVIEW)):
            ts = html.escape(str(rec.get("timestamp", "?")))
            cmd = html.escape(str(rec.get("command", "?")))
            err = html.escape(str(rec.get("error", "")))
            lines.append(f"\n🕒 <i>{ts}</i>  <code>{cmd}</code>\n{err}")
        if new > _PUSH_ERROR_PREVIEW:
            lines.append(f"\n…and {new - _PUSH_ERROR_PREVIEW} more. Send /errors to see them.")
        return "\n".join(lines)

    def notify_new_errors(self) -> bool:
        """Send the end-of-run error push to the admin chat. Returns True if sent.

        No-op (returns False) when there are no new errors or no admin chat is
        configured. Call this once, AFTER poll_once(), at the end of the run.
        """
        if self.admin_chat_id is None:
            return False
        summary = self.summarize_new_errors()
        if summary is None:
            return False
        send_message(self.token, self.admin_chat_id, summary, parse_mode="HTML")
        return True
