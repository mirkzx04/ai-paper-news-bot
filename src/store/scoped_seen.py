"""ScopedSeenStore — per-user "seen" view over one shared Store.

The global ``seen`` table is correct for a single user but wrong for multi-user
delivery: if user A's digest marks paper X seen, user B (whose profile also
matches X) would never receive it. Each user must have an independent seen-set.

This wrapper gives every user their own seen namespace WITHOUT a schema change:
it prefixes the canonical key with the user id (``<user_id>::<canonical_key>``)
on ``is_seen``/``mark_seen``, while delegating the small key/value ``meta`` store
(Telegram offset, per-user ``last_digest_at`` keys, flow state) straight through
to the shared base store. ``close()`` is a deliberate no-op — the fan-out owns the
base store's lifecycle and reuses it across every user in the run.

``conn`` is forwarded so callers that need the underlying SQLite connection (e.g.
``app._store_db_path`` building the shared ``SentItemsStore``) keep working.
"""

from __future__ import annotations

from datetime import datetime

from src.store.base import Store


class ScopedSeenStore(Store):
    def __init__(self, base: Store, user_id: str) -> None:
        self._base = base
        self._prefix = f"{user_id}::"

    @property
    def conn(self):
        # Forward the underlying connection for helpers that read it directly.
        return self._base.conn

    def _scoped(self, key: str) -> str:
        return self._prefix + key

    def is_seen(self, key: str) -> bool:
        return self._base.is_seen(self._scoped(key))

    def mark_seen(self, key: str, when: datetime) -> None:
        self._base.mark_seen(self._scoped(key), when)

    def mark_seen_many(self, keys: list[str], when: datetime) -> None:
        # Prefix every key, then let the base store batch them in one transaction.
        self._base.mark_seen_many([self._scoped(k) for k in keys], when)

    def get_meta(self, key: str) -> str | None:
        return self._base.get_meta(key)

    def set_meta(self, key: str, value: str) -> None:
        self._base.set_meta(key, value)

    def close(self) -> None:
        # The shared base store outlives this per-user view; don't close it here.
        pass
