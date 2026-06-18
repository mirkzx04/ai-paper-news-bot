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
            records: list = []
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, list):
                    records = loaded
            except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
                records = []

            records.append(record)

            # Ensure the parent directory exists before writing back.
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - logging must never crash the bot
            logger.warning("failed to record error to %s: %s", self.path, exc)
