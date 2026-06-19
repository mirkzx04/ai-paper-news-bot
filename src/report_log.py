"""`ReportLog` — append user reports to a JSON file.

Each report is stored as a record `{"timestamp": <UTC ISO8601>, "report": <text>}`
inside a JSON list. Reads are defensive: a missing, corrupt, or non-list file is
treated as an empty list, and the class NEVER raises (failures are logged).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ReportLog:
    def __init__(self, path: str = "data/reports.json") -> None:
        self.path = Path(path)

    def _read(self) -> list:
        """Load the existing report list; return [] on any read/parse problem."""
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            # No file yet — start from an empty list.
            return []
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt or unreadable file — log and fall back to empty list.
            logger.warning("Could not read report log %s: %s", self.path, exc)
            return []

        # Guard against a JSON payload that is not a list (e.g. {} or a string).
        if not isinstance(data, list):
            logger.warning("Report log %s is not a list; resetting", self.path)
            return []
        return data

    def recent(self, n: int = 10) -> list:
        """Return the most recent `n` report records (newest last in the file,
        so the tail is the latest). Missing/corrupt file -> []. NEVER raises.

        Records keep their on-disk shape ``{"timestamp", "report"}``. ``n <= 0``
        returns []; ``n`` larger than the log returns every record.
        """
        if n <= 0:
            return []
        return self._read()[-n:]

    def count(self) -> int:
        """Total number of stored reports. Missing/corrupt file -> 0. NEVER raises."""
        return len(self._read())

    def add(self, text: str) -> None:
        """Append a timestamped report record to the JSON list. NEVER raises."""
        try:
            records = self._read()
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "report": text,
            }
            records.append(record)

            # Ensure the parent directory exists before writing.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 — must never propagate to the caller.
            logger.error("Failed to write report to %s: %s", self.path, exc)
