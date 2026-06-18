"""CommandDispatcher — parse a message and route it to the right Command.

Handles the Telegram quirks: a command can arrive as "/add_author@botname args"
and is case-insensitive on the command name. Non-command text returns None
(the caller just skips it). Unknown commands and /start//help return usage text.
"""

from __future__ import annotations

import logging

from src.commands.base import Command
from src.store.profile_store import ProfileStore

logger = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(self, commands: list[Command], store: ProfileStore) -> None:
        self.store = store
        self.commands: dict[str, Command] = {c.name: c for c in commands}

    def dispatch(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text.startswith("/"):
            return None
        head, _, args = text[1:].partition(" ")
        name = head.split("@", 1)[0].lower()  # strip "@botname" suffix
        if name in ("start", "help"):
            return self.help_text()
        command = self.commands.get(name)
        if command is None:
            return f"Comando sconosciuto: /{name}\n\n{self.help_text()}"
        try:
            return command.handle(args.strip(), self.store)
        except Exception as exc:  # a bad command must not crash the poll loop
            logger.warning("command /%s failed: %s", name, exc)
            return f"⚠️ Errore eseguendo /{name}."

    def help_text(self) -> str:
        lines = ["Comandi disponibili:"]
        for command in self.commands.values():
            lines.append(f"/{command.name} — {command.description}")
        return "\n".join(lines)
