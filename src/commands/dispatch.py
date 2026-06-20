"""CommandDispatcher — parse a message and route it to the right Command.

Handles the Telegram quirks: a command can arrive as "/add_author@botname args"
and is case-insensitive on the command name. Non-command text returns None
(the caller just skips it). Unknown commands and /start//help return usage text.
"""

from __future__ import annotations

import logging
import traceback

from src.commands.base import Command
from src.error_log import ErrorLog
from src.store.profile_store import ProfileStore

logger = logging.getLogger(__name__)

# Shown on /start.
WELCOME = (
    "👋 Welcome! I surface new AI research papers that match your interests.\n\n"
    "Right now I'm connected to the arXiv API only — more sources are on the way.\n\n"
    "• Run /creare_profile to set up your profile: read papers, authors, and topics.\n"
    "• Manage it anytime with the /add_* and /remove_* commands.\n"
    "• Run /help to see all available commands."
)


class CommandDispatcher:
    def __init__(
        self,
        commands: list[Command],
        store: ProfileStore,
        error_log: ErrorLog | None = None,
    ) -> None:
        self.store = store
        self.commands: dict[str, Command] = {c.name: c for c in commands}
        # Sink for command failures; default to a fresh ErrorLog if not provided.
        self.error_log = error_log or ErrorLog()

    def dispatch(self, text: str, store: ProfileStore | None = None) -> str | None:
        text = (text or "").strip()
        if not text.startswith("/"):
            return None
        head, _, args = text[1:].partition(" ")
        name = head.split("@", 1)[0].lower()  # strip "@botname" suffix
        if name == "start":
            return WELCOME
        if name == "help":
            return self.help_text()
        command = self.commands.get(name)
        if command is None:
            return f"Unknown command: /{name}\n\n{self.help_text()}"
        try:
            return command.handle(args.strip(), store or self.store)
        except Exception as exc:  # a bad command must not crash the poll loop
            logger.warning("command /%s failed: %s", name, exc)
            self.error_log.record(
                command=f"/{name}",
                args=args,
                error=repr(exc),
                traceback_str=traceback.format_exc(),
            )
            return "Command execution failed"

    def help_text(self) -> str:
        lines = ["Available commands:"]
        for command in self.commands.values():
            lines.append(f"/{command.name} — {command.description}")
        return "\n".join(lines)
