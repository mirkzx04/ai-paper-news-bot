"""SQLite-backed store for local development.

Tracks which items we've already evaluated so they aren't re-scored or
re-notified across runs. The CI deployment will use a JSON-on-branch store with
the same `Store` interface.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from src.store.base import Store


class SqliteStore(Store):
    def __init__(self, path: str = "data/bot.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "  key TEXT PRIMARY KEY,"
            "  first_seen TEXT NOT NULL"
            ")"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        self.conn.commit()

    def is_seen(self, key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
        return cur.fetchone() is not None

    def mark_seen(self, key: str, when: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (key, first_seen) VALUES (?, ?)",
            (key, when.isoformat()),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        cur = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
