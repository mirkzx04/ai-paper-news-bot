"""Standalone smoke test for the 👍/👎 feedback round-trip against the real Bot API.

Lets the owner do a quick LIVE check of the inline-feedback loop WITHOUT running
the whole cron/pipeline: send one fake-paper message carrying the 👍/👎 inline
buttons, then poll getUpdates to watch the vote (a callback_query) come back.

  # 1) post a fake paper with 👍/👎 buttons (prints the message_id):
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python tools/telegram_smoke.py send

  # 2) tap a button in Telegram, then read the callback_query back:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python tools/telegram_smoke.py poll

This deliberately reuses the production HTTP helpers and the production
callback_data format so what you exercise here is exactly what the cron run
emits/parses — nothing about the feedback wire format is re-implemented locally.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make the repo root importable when run as `tools/telegram_smoke.py` (mirrors
# the sibling tools/telegram_setup.py convention).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.env import load_env  # noqa: E402
# Thin Bot API helpers — the SINGLE source of the HTTP calls; we never POST here.
from src.telegram_api import (  # noqa: E402
    answer_callback_query,
    get_updates,
    send_message,
)
# Reuse the EXACT production feedback wire format instead of hard-coding the
# "fb:u:"/"fb:d:" literals: the prefix constants live on the notifier and the
# token derivation on the sent-items store. Importing the underscore-prefixed
# prefixes (module-private by convention) is intentional — it keeps this smoke
# test in lock-step with what TelegramNotifier emits and TelegramPoller parses,
# so a future change to the format can't silently desync this tool.
from src.notify.telegram_notifier import (  # noqa: E402
    _FEEDBACK_DOWN_PREFIX,
    _FEEDBACK_UP_PREFIX,
)
from src.store.sent_items_store import token_for_key  # noqa: E402

# A throwaway canonical key for the fake paper. The vote it produces is written
# under this key by a real run's poller, so keep it obviously synthetic.
_FAKE_CANONICAL_KEY = "arxiv:0000.00000"


def _require_env() -> tuple[str, str]:
    """Read TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, or exit non-zero with a clear
    message naming exactly what's missing (so the owner knows what to export)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    missing = [
        name
        for name, val in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
        if not val
    ]
    if missing:
        print(
            "Missing required env var(s): "
            + ", ".join(missing)
            + ".\nSet them in the environment or in a local .env file, e.g.:\n"
            "  export TELEGRAM_BOT_TOKEN=123456:ABC...\n"
            "  export TELEGRAM_CHAT_ID=987654321",
            file=sys.stderr,
        )
        sys.exit(2)
    return token, chat_id  # type: ignore[return-value]  # narrowed by the guard above


def _feedback_markup(canonical_key: str) -> dict:
    """Single-row inline keyboard 👍 / 👎 — byte-identical to TelegramNotifier's.

    Built from the imported production prefixes + token_for_key so the
    callback_data round-trips through TelegramPoller._parse_feedback_data exactly
    as a real notification would.
    """
    token = token_for_key(canonical_key)
    return {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": _FEEDBACK_UP_PREFIX + token},
                {"text": "👎", "callback_data": _FEEDBACK_DOWN_PREFIX + token},
            ]
        ]
    }


def _cmd_send() -> int:
    """Send one fake-paper message WITH the 👍/👎 buttons; print its message_id."""
    token, chat_id = _require_env()
    markup = _feedback_markup(_FAKE_CANONICAL_KEY)
    text = (
        "🧪 <b>SMOKE TEST</b> — fake paper for the feedback round-trip\n\n"
        "📄 <b>Title:</b> A Study of Nothing in Particular\n"
        "👤 <b>Authors:</b> Smoke Tester\n"
        f"🔑 <b>key:</b> <code>{_FAKE_CANONICAL_KEY}</code>\n\n"
        "Tap 👍 or 👎 below, then run "
        "<code>tools/telegram_smoke.py poll</code> to see the vote arrive."
    )
    print(f"Button callback_data: {markup['inline_keyboard'][0][0]['callback_data']!r} / "
          f"{markup['inline_keyboard'][0][1]['callback_data']!r}")
    try:
        resp = send_message(token, chat_id, text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — surface any transport error plainly
        print(f"send_message raised: {exc!r}", file=sys.stderr)
        return 1

    print(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print(f"Non-JSON response body: {resp.text[:300]}", file=sys.stderr)
        return 1
    if not body.get("ok"):
        print(f"Telegram returned not-ok: {body}", file=sys.stderr)
        return 1
    message_id = body.get("result", {}).get("message_id")
    print(f"Sent. message_id = {message_id}")
    print("Now vote in Telegram, then: tools/telegram_smoke.py poll")
    return 0


def _cmd_poll() -> int:
    """getUpdates once and print any callback_query (the vote) and messages.

    Every callback_query is acknowledged with answerCallbackQuery so the client's
    loading spinner stops — same as the real poller does. This advances no
    offset, so a callback may show again on the next poll until the poller proper
    (or another getUpdates with offset) consumes it.
    """
    token, _chat_id = _require_env()
    try:
        data = get_updates(token, offset=None, timeout=0)
    except Exception as exc:  # noqa: BLE001 — surface any transport error plainly
        print(f"get_updates raised: {exc!r}", file=sys.stderr)
        return 1

    if not data.get("ok"):
        print(f"getUpdates returned not-ok: {data}", file=sys.stderr)
        return 1

    updates = data.get("result", [])
    print(f"Got {len(updates)} update(s).")
    if not updates:
        print("No pending updates. Did you tap a button after `send`? Note: an "
              "earlier cron poll may have already consumed (offset-advanced) it.")
        return 0

    seen_callback = False
    for update in updates:
        update_id = update.get("update_id")
        callback = update.get("callback_query")
        if callback is not None:
            seen_callback = True
            cq_id = callback.get("id")
            cq_data = callback.get("data")
            sender = callback.get("from") or {}
            who = sender.get("username") or sender.get("first_name") or sender.get("id")
            msg = callback.get("message") or {}
            message_id = msg.get("message_id")
            print(
                f"[update {update_id}] CALLBACK_QUERY  data={cq_data!r}  "
                f"from={who!r}  message_id={message_id}"
            )
            # Stop the spinner on the user's side (best-effort; never raises).
            if cq_id is not None:
                acked = answer_callback_query(token, cq_id, text="✅ smoke test received")
                print(f"    answered callback {cq_id} -> ok={acked}")
            continue

        message = update.get("message") or update.get("edited_message") or {}
        if message:
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            who = sender.get("username") or sender.get("first_name") or sender.get("id")
            print(
                f"[update {update_id}] MESSAGE  text={message.get('text')!r}  "
                f"from={who!r}  chat_id={chat.get('id')}  "
                f"message_id={message.get('message_id')}"
            )
            continue

        print(f"[update {update_id}] (other update type): {sorted(update.keys())}")

    if not seen_callback:
        print("No callback_query in this batch — tap 👍/👎 on the smoke message, "
              "then poll again.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools/telegram_smoke.py",
        description=(
            "Quick LIVE smoke test of the 👍/👎 feedback round-trip against the "
            "Bot API (no cron/pipeline). Reads TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID from the environment (or a local .env)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  tools/telegram_smoke.py send   # post a fake paper with 👍/👎 buttons\n"
            "  tools/telegram_smoke.py poll   # read the vote (callback_query) back\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="{send,poll}")
    sub.add_parser("send", help="send one fake-paper message with 👍/👎 inline buttons")
    sub.add_parser("poll", help="getUpdates once and print callback_query/messages")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()  # local .env convenience; real env always wins (see src.env)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "send":
        return _cmd_send()
    if args.command == "poll":
        return _cmd_poll()

    # No subcommand: print usage and exit non-zero (nothing to do).
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
