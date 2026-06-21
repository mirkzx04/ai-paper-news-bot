"""Thin Telegram Bot API helpers (stateless HTTP), shared by poller and tools."""

from __future__ import annotations

import json
import logging

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}/{method}"

# HTTP statuses Telegram returns for a chat that can never be delivered to again
# (the user blocked the bot, deactivated their account, or the chat was deleted).
# These are PERMANENT, not transient: a registry should mark the user blocked
# rather than retry. 400 is included only for the specific "chat not found" body,
# handled by the caller; here we treat 403 as the unambiguous permanent signal.
PERMANENT_SEND_STATUSES = frozenset({403})


class PermanentSendError(Exception):
    """A send failed permanently — the chat is unreachable (e.g. user blocked bot).

    Carries the offending ``status`` and ``chat_id`` so a caller (the per-user
    digest fan-out) can mark that user ``blocked`` in the registry and stop
    sending to them, distinct from a transient failure that should be retried.
    """

    def __init__(self, status: int, chat_id, detail: str = "") -> None:
        self.status = status
        self.chat_id = chat_id
        self.detail = detail
        super().__init__(f"permanent send failure {status} to chat {chat_id}: {detail}")


def send_message(token: str, chat_id, text: str, parse_mode: str | None = None,
                 reply_markup: dict | None = None,
                 timeout: int = 20) -> requests.Response:
    """POST sendMessage. Returns the raw `requests.Response`.

    `reply_markup` (optional) is any Telegram reply-markup object — e.g. an
    inline keyboard ``{"inline_keyboard": [[...]]}`` for the 👍/👎 feedback
    buttons. It is JSON-serialised into the form field as the Bot API requires;
    omitting it leaves the payload byte-for-byte identical to before, so every
    existing caller is unaffected. The caller can read ``resp.json()["result"]
    ["message_id"]`` from the return value when it needs the sent message id.
    """
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return requests.post(_BASE.format(token=token, method="sendMessage"),
                         json=payload, timeout=timeout)


def answer_callback_query(token: str, callback_query_id: str, text: str | None = None,
                          timeout: int = 10) -> bool:
    """Best-effort answerCallbackQuery — stops the client's loading spinner.

    Telegram requires every ``callback_query`` to be answered; until then the
    inline button shows a spinner on the user's side. `text` is an optional
    toast shown briefly to the user (e.g. "👍 registrato").

    Never raises: a feedback ack failing must not break the poll loop. Returns
    True only on a confirmed ``ok`` from the API, False otherwise.
    """
    payload: dict = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    try:
        resp = requests.post(_BASE.format(token=token, method="answerCallbackQuery"),
                             json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("answerCallbackQuery error: %s", exc)
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("ok"))
    except ValueError:
        return False


def get_updates(token: str, offset: int | None = None, timeout: int = 0,
                req_timeout: int = 25) -> dict:
    """GET getUpdates. `timeout` is Telegram's long-poll (seconds): with
    ``timeout > 0`` the server holds the connection open until an update arrives
    or `timeout` elapses; ``timeout = 0`` (the default) returns immediately.

    `req_timeout` is the HTTP read timeout handed to `requests`. When long-
    polling it MUST exceed `timeout`, otherwise `requests` aborts the request
    before Telegram replies. We defensively clamp it up to ``timeout + 5`` so a
    caller that forgets (or passes a too-small `req_timeout`) still works; with
    ``timeout = 0`` this is a no-op and the HTTP timeout is left untouched.
    """
    if timeout > 0:
        req_timeout = max(req_timeout, timeout + 5)
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(_BASE.format(token=token, method="getUpdates"),
                        params=params, timeout=req_timeout)
    return resp.json()


def set_my_commands(token: str, commands: list[dict], timeout: int = 20) -> requests.Response:
    """Register the bot's slash-command menu. `commands` = [{command, description}, ...]."""
    return requests.post(_BASE.format(token=token, method="setMyCommands"),
                         json={"commands": commands}, timeout=timeout)


def delete_message(token: str, chat_id, message_id: int, timeout: int = 10) -> bool:
    """Best-effort deleteMessage. Returns True on a successful API call, False otherwise.

    Never raises: deleting a message that the bot can't touch (someone else's
    message, or older than 48h) is expected to fail, so the caller can keep going.
    """
    try:
        resp = requests.post(_BASE.format(token=token, method="deleteMessage"),
                             json={"chat_id": chat_id, "message_id": message_id},
                             timeout=timeout)
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("ok"))
    except ValueError:
        return False


def edit_message_reply_markup(token: str, chat_id, message_id: int,
                              reply_markup: dict | None,
                              timeout: int = 10) -> bool:
    """Best-effort editMessageReplyMarkup — swap a sent message's inline keyboard.

    Used by the 👍/👎 feedback loop to re-render the buttons after a vote so the
    chosen option is visibly marked (an "affordance" the user voted). The
    `reply_markup` is JSON-serialised as the Bot API requires; pass ``None`` to
    strip the keyboard entirely.

    Returns True only on a confirmed ``ok`` from the API, False otherwise. NEVER
    raises: editing a message that's too old, already gone, or otherwise
    un-editable is expected to fail, and a failed cosmetic edit must never break
    the poll loop — the vote itself is already recorded by the caller.
    """
    payload: dict = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(_BASE.format(token=token, method="editMessageReplyMarkup"),
                             json=payload, timeout=timeout)
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("ok"))
    except ValueError:
        return False
