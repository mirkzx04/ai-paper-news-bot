"""ErrorLog — durable append-only history for runtime failures.

Errors are written as JSON Lines (one JSON object per line) instead of rewriting a
single JSON list. Appending is O(1), a corrupt line costs only that one record,
and existing deployments keep their old ``data/error_log.json`` history because
the default reader folds that legacy file into the new ``data/error_log.jsonl``.

Recording an error must never crash the bot: every failure inside ``record`` is
swallowed and reported through the module logger instead of propagating.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)
_REQUIRED_KEYS = {"timestamp", "command", "error"}


def _is_error_record(record) -> bool:
    return isinstance(record, dict) and _REQUIRED_KEYS.issubset(record)


class ErrorLog:
    def __init__(self, path: str = "data/error_log.jsonl",
                 legacy_path: str | None = None) -> None:
        self.path = Path(path)
        if legacy_path is None and self.path.suffix == ".jsonl":
            legacy_path = str(self.path.with_suffix(".json"))
        self.legacy_path = Path(legacy_path) if legacy_path else None

    def _read_json_list(self, path: Path) -> list[dict] | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return None
        if not isinstance(loaded, list):
            return None
        return [rec for rec in loaded if _is_error_record(rec)]

    def _read_jsonl(self, path: Path) -> list[dict]:
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        loaded = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("skipping corrupt error-log line in %s", path)
                        continue
                    if _is_error_record(loaded):
                        out.append(loaded)
        except FileNotFoundError:
            return out
        except OSError as exc:
            logger.warning("could not read error log %s: %s", path, exc)
            return out
        return out

    def _read_file(self, path: Path) -> list[dict]:
        """Read either legacy JSON-list or current JSONL content from ``path``."""
        legacy_records = self._read_json_list(path)
        if legacy_records is not None:
            return legacy_records
        return self._read_jsonl(path)

    def _read(self) -> list[dict]:
        """Load legacy + current error records. Missing/corrupt files never raise."""
        records: list[dict] = []
        if self.legacy_path is not None and self.legacy_path != self.path:
            records.extend(self._read_file(self.legacy_path))
        records.extend(self._read_file(self.path))
        return records

    def recent(self, n: int = 10) -> list[dict]:
        """Return the most recent ``n`` error records (newest last). NEVER raises."""
        if n <= 0:
            return []
        return self._read()[-n:]

    def count(self) -> int:
        """Total number of recorded errors. Missing/corrupt file -> 0. NEVER raises."""
        return len(self._read())

    def _write_jsonl_atomic(self, path: Path, records: Iterable[dict]) -> None:
        """Replace ``path`` with JSONL ``records``; used only for legacy migration."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _migrate_current_path_if_legacy_list(self) -> None:
        """Convert a valid JSON-list log at ``self.path`` to JSONL before append."""
        records = self._read_json_list(self.path)
        if records is not None:
            self._write_jsonl_atomic(self.path, records)

    def _append_jsonl(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_current_path_if_legacy_list()
        line = json.dumps(record, ensure_ascii=False)

        needs_leading_newline = False
        try:
            if self.path.exists() and self.path.stat().st_size > 0:
                with self.path.open("rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    needs_leading_newline = fh.read(1) != b"\n"
        except OSError:
            needs_leading_newline = False

        with self.path.open("a", encoding="utf-8") as fh:
            if needs_leading_newline:
                fh.write("\n")
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def record(
        self,
        command: str,
        args: str,
        error: str,
        traceback_str: str | None = None,
    ) -> None:
        """Append one error record to the JSONL history. NEVER raises."""
        try:
            self._append_jsonl({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "args": args,
                "error": error,
                "traceback": traceback_str,
            })
        except Exception as exc:  # noqa: BLE001 - logging must never crash the bot
            logger.warning("failed to record error to %s: %s", self.path, exc)
