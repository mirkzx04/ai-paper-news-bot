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

from src.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


# Anti-flood caps for a public bot: bound how many reports a single anonymous
# user can accumulate, and drop a report identical to that user's most recent one.
_MAX_REPORTS_PER_USER = 50


class ReportLog:
    def __init__(self, path: str = "data/reports.json",
                 max_per_user: int = _MAX_REPORTS_PER_USER) -> None:
        self.path = Path(path)
        self.max_per_user = int(max_per_user)

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

    def add(self, text: str, user_id: str | None = None) -> bool:
        """Append a timestamped report record to the JSON list. NEVER raises.

        `user_id` (optional anonymous ``u_<id>``) enables anti-flood guards for a
        public bot: a report identical to that user's most recent one is dropped
        (dedup), and a user already at ``max_per_user`` reports is rejected. The
        on-disk record stays backward compatible — ``user_id`` is added only when
        supplied. Returns True if the report was stored, False if it was dropped by
        a guard or a write error.
        """
        try:
            records = self._read()
            if user_id is not None:
                user_records = [r for r in records
                                if isinstance(r, dict) and r.get("user_id") == user_id]
                # Dedup: skip an exact repeat of this user's latest report.
                if user_records and str(user_records[-1].get("report", "")) == str(text):
                    logger.info("report: dropping duplicate from %s", user_id)
                    return False
                # Cap: refuse once a user has flooded the log.
                if len(user_records) >= self.max_per_user:
                    logger.warning("report: %s at cap (%d); dropping", user_id, self.max_per_user)
                    return False
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "report": text,
            }
            if user_id is not None:
                record["user_id"] = user_id
            records.append(record)

            # Atomic: rewriting the whole list in place would lose every prior
            # report if the write were interrupted partway.
            atomic_write_text(self.path, json.dumps(records, indent=2, ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001 — must never propagate to the caller.
            logger.error("Failed to write report to %s: %s", self.path, exc)
            return False
