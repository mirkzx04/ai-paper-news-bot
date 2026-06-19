"""ErrorLog — append command failures to a JSON file on disk.

Recording an error must never crash the bot: every failure inside `record`
(disk errors, serialization issues, ...) is swallowed and reported through the
module logger instead of propagating to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ErrorLog:
    def __init__(self, path: str = "data/error_log.json") -> None:
        # Path of the JSON file holding the list of recorded errors.
        self.path = path

    def _read(self) -> list:
        """Load the stored error list; return [] on any read/parse problem.

        Tolerates a missing file, corrupt JSON, or a JSON value that is not a
        list. Never raises — every failure degrades to an empty list.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return []
        if not isinstance(loaded, list):
            return []
        return loaded

    def recent(self, n: int = 10) -> list:
        """Return the most recent `n` error records (the file's tail is newest).

        Missing/corrupt file -> []. ``n <= 0`` returns []; ``n`` larger than the
        log returns every record. Records keep their on-disk shape
        ``{"timestamp", "command", "args", "error", "traceback"}``. NEVER raises.
        """
        if n <= 0:
            return []
        return self._read()[-n:]

    def count(self) -> int:
        """Total number of recorded errors. Missing/corrupt file -> 0. NEVER raises."""
        return len(self._read())

    def record(
        self,
        command: str,
        args: str,
        error: str,
        traceback_str: str | None = None,
    ) -> None:
        """Append one error record to the JSON list stored at `self.path`.

        This method is intentionally exception-proof: a logging failure must
        never take down the bot. Any error is caught and logged as a warning.
        """
        try:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "args": args,
                "error": error,
                "traceback": traceback_str,
            }

            # Read the existing content, tolerating any of: missing file,
            # corrupt JSON, or a JSON value that is not a list. In all those
            # cases we start from an empty list and never raise on read.
            records: list = self._read()

            records.append(record)

            # Ensure the parent directory exists before writing back.
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - logging must never crash the bot
            logger.warning("failed to record error to %s: %s", self.path, exc)
