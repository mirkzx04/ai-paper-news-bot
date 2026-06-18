"""`/report` — let users report bugs, inaccuracies, or feature requests.

The report text is appended to a JSON log via `ReportLog`. The command owns its
own `ReportLog` (injectable for testing) and therefore ignores the `store` arg.

NOTE: the user-facing strings below are English placeholders — a copywriter will
finalize the wording later.
"""

from __future__ import annotations

from src.commands.base import Command
from src.report_log import ReportLog
from src.store.profile_store import ProfileStore


class ReportCommand(Command):
    name = "report"
    description = "Report a bug, an inaccuracy, or request a feature"

    def __init__(self, report_log: ReportLog | None = None) -> None:
        # Use the injected log if provided, otherwise the default-path one.
        self.report_log = report_log or ReportLog()

    def handle(self, args: str, store: ProfileStore) -> str:
        # `store` is intentionally unused — this command persists via ReportLog.
        text = args.strip()
        if not text:
            return "Usage: /report <your message> — describe the bug, inaccuracy, or feature you have in mind."

        self.report_log.add(text)
        return "Thanks! Your report has been received and saved — I appreciate the feedback."
