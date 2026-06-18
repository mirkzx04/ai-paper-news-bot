"""Telegram setup helper — find your chat_id and/or send a test message.

  # 1) scrivi un messaggio qualsiasi al tuo bot su Telegram, poi:
  TELEGRAM_BOT_TOKEN=... python tools/telegram_setup.py
  #    -> stampa le chat_id viste di recente

  # 2) verifica l'invio:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python tools/telegram_setup.py --send-test
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.env import load_env  # noqa: E402


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Imposta TELEGRAM_BOT_TOKEN (env o .env).")
        return
    base = f"https://api.telegram.org/bot{token}"

    if args.send_test:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not chat_id:
            print("Imposta TELEGRAM_CHAT_ID per il test.")
            return
        resp = requests.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ paper-news-bot connesso."},
            timeout=20,
        )
        print(resp.status_code, resp.text[:300])
        return

    resp = requests.get(f"{base}/getUpdates", timeout=20)
    data = resp.json()
    if not data.get("ok"):
        print("getUpdates error:", data)
        return
    chats: dict = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat", {})
        if chat:
            chats[chat.get("id")] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("Nessun messaggio recente. Scrivi qualcosa al bot su Telegram e rilancia.")
        return
    print("Chat trovate (usa l'id come TELEGRAM_CHAT_ID):")
    for chat_id, name in chats.items():
        print(f"  {chat_id}  {name}")


if __name__ == "__main__":
    main()
