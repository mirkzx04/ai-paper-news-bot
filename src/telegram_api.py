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
