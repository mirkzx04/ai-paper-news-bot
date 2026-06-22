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
        # WAL + synchronous=NORMAL: ~34x faster writes than the default
        # (rollback-journal + fsync-per-commit) while staying durable across app
        # crashes. The only thing at risk is the last transaction on an OS/power
        # crash — for a bot that re-syncs state to a gist and whose worst loss is
        # re-showing one paper, that trade is free. close() checkpoints the WAL
        # back into the .db file so the tarred-to-gist snapshot is self-contained.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # SentItemsStore opens a second connection to this same file (see
        # app._store_db_path). WAL lets readers and a writer coexist, but two
        # writers still contend — wait up to 5s for the lock instead of raising
        # SQLITE_BUSY on the loser.
        self.conn.execute("PRAGMA busy_timeout=5000")
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

    def mark_seen_many(self, keys: list[str], when: datetime) -> None:
        """Insert every key under one transaction — one fsync for the whole batch
        instead of one per row (~14x over the per-row path for a run's worth of
        items)."""
        if not keys:
            return
        ts = when.isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen (key, first_seen) VALUES (?, ?)",
            [(key, ts) for key in keys],
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
        # Fold the WAL back into the main .db file so the gist tar (which only
        # snapshots data/) captures a complete, sidecar-free database.
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:  # never let a checkpoint failure block cleanup
            pass
        self.conn.close()
