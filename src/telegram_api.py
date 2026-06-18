"""Thin Telegram Bot API helpers (stateless HTTP), shared by poller and tools."""

from __future__ import annotations

import requests

_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_message(token: str, chat_id, text: str, parse_mode: str | None = None,
                 timeout: int = 20) -> requests.Response:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return requests.post(_BASE.format(token=token, method="sendMessage"),
                         json=payload, timeout=timeout)


def get_updates(token: str, offset: int | None = None, timeout: int = 0,
                req_timeout: int = 25) -> dict:
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
