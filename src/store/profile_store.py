"""ProfileStore — mutable overlay of user-added interests (JSON-backed).

Kept SEPARATE from `config/profile.yaml`: the YAML stays the human-edited seed
(with its comments), while everything the user adds at runtime via bot commands
(`/add_author`, `/add_keywords`, `/add_topic`, `/add_conference`) lands here and
is merged on top at load time (see `config.apply_profile_overlay`). This matches
the planned deployment where mutable state is JSON committed to a `state` branch.

All mutations dedup case-insensitively while preserving the user's casing, and
auto-save. Mutators return the items that were *newly* added, so command
handlers can give precise replies ("added X", "already present: Y").

An OPTIONAL observer (``listener``) can be injected to react to *real* changes:
a ``Callable[[str, str, str], None]`` invoked once per item actually added or
removed, with ``(action, kind, value)`` where ``action`` is ``"add"|"remove"``
and ``kind`` is ``"author"|"keyword"|"topic"|"conference"|"seed"``. It defaults
to ``None``, in which case the store behaves EXACTLY as before. This is how
preference signals are forwarded to the append-only `PreferenceDataset` without
coupling this store to it (the concrete listener lives in `preference_dataset`).
A faulty listener can never corrupt the overlay or propagate to the bot: every
notification is wrapped and failures are only logged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Observer signature: (action, kind, value) -> None.
ProfileListener = Callable[[str, str, str], None]


class ProfileStore:
    _LIST_KEYS = ("authors", "keywords", "conferences")
    # Maps the internal storage key to the singular `kind` used in notifications.
    _KEY_TO_KIND = {"authors": "author", "keywords": "keyword",
                    "conferences": "conference", "seeds": "seed"}

    def __init__(
        self,
        path: str = "data/profile_overlay.json",
        listener: Optional[ProfileListener] = None,
    ) -> None:
        self.path = path
        # Optional observer; None => no notifications, identical to prior behaviour.
        self._listener = listener
        self._data: dict = {"authors": [], "keywords": [], "topics": {},
                            "conferences": [], "seeds": []}
        self._load()

    # ---- observer ----------------------------------------------------------
    def _notify(self, action: str, kind: str, value: str) -> None:
        """Invoke the injected listener for one real change. NEVER raises.

        Persisting the user's profile must not depend on, nor be broken by, the
        observer: any listener error is swallowed and logged.
        """
        if self._listener is None:
            return
        try:
            self._listener(action, kind, value)
        except Exception as exc:  # noqa: BLE001 — observer must never break a mutation
            logger.warning("profile listener failed on %s/%s: %s", action, kind, exc)

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        for key in self._data:
            if key in loaded:
                self._data[key] = loaded[key]

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)

    # ---- accessors (used by the config overlay merge) ----------------------
    @property
    def authors(self) -> list[str]:
        return list(self._data["authors"])

    @property
    def keywords(self) -> list[str]:
        return list(self._data["keywords"])

    @property
    def conferences(self) -> list[str]:
        return list(self._data["conferences"])

    @property
    def seeds(self) -> list[str]:
        return list(self._data["seeds"])

    @property
    def topics(self) -> dict[str, list[str]]:
        return {name: list(kws) for name, kws in self._data["topics"].items()}

    # ---- mutations ---------------------------------------------------------
    def add_authors(self, names: list[str]) -> list[str]:
        return self._add_to_list("authors", names)

    def add_keywords(self, keywords: list[str]) -> list[str]:
        return self._add_to_list("keywords", keywords)

    def add_conferences(self, names: list[str]) -> list[str]:
        return self._add_to_list("conferences", names)

    def add_seed_ids(self, arxiv_ids: list[str]) -> list[str]:
        return self._add_to_list("seeds", arxiv_ids)

    def add_topic(self, name: str, keywords: list[str]) -> tuple[bool, list[str]]:
        """Create/extend a topic. Returns (topic_was_created, newly_added_keywords)."""
        name = name.strip()
        if not name:
            return (False, [])
        topics = self._data["topics"]
        existing_key = next((k for k in topics if k.lower() == name.lower()), None)
        created = existing_key is None
        key = existing_key or name
        topics.setdefault(key, [])
        added = _append_unique(topics[key], keywords)
        if created or added:
            self._save()
        # Notify per added keyword (the keywords are the preference signal). If
        # the topic was created with no keywords there is no per-keyword value,
        # so emit a single event carrying the topic name to avoid losing it.
        for kw in added:
            self._notify("add", "topic", kw)
        if created and not added:
            self._notify("add", "topic", key)
        return (created, added)

    def remove_authors(self, names: list[str]) -> list[str]:
        return self._remove_from_list("authors", names)

    def remove_keywords(self, keywords: list[str]) -> list[str]:
        return self._remove_from_list("keywords", keywords)

    def remove_conferences(self, names: list[str]) -> list[str]:
        return self._remove_from_list("conferences", names)

    def remove_topic(self, name: str, keywords: list[str]) -> tuple[str, list[str]]:
        """Remove a whole topic (no keywords given) or specific keywords from it.

        Returns one of:
          ("not_found", [])            -> the topic doesn't exist
          ("topic_removed", [])        -> the whole topic was deleted
          ("keywords_removed", [...])  -> the listed keywords removed from the topic
                                          (the list is empty if none matched)
        """
        name = name.strip()
        topics = self._data["topics"]
        existing_key = next((k for k in topics if k.lower() == name.lower()), None)
        if existing_key is None:
            return ("not_found", [])
        if not keywords:
            removed_kws = list(topics[existing_key])
            del topics[existing_key]
            self._save()
            # Removing a whole topic withdraws each of its keywords as a signal;
            # if it had none, emit one event with the topic name (mirrors add).
            for kw in removed_kws:
                self._notify("remove", "topic", kw)
            if not removed_kws:
                self._notify("remove", "topic", existing_key)
            return ("topic_removed", [])
        removed = _remove_unique(topics[existing_key], keywords)
        if removed:
            self._save()
        for kw in removed:
            self._notify("remove", "topic", kw)
        return ("keywords_removed", removed)

    # ---- internals ---------------------------------------------------------
    def _add_to_list(self, key: str, items: list[str]) -> list[str]:
        added = _append_unique(self._data[key], items)
        if added:
            self._save()
        kind = self._KEY_TO_KIND[key]
        for value in added:
            self._notify("add", kind, value)
        return added

    def _remove_from_list(self, key: str, items: list[str]) -> list[str]:
        removed = _remove_unique(self._data[key], items)
        if removed:
            self._save()
        kind = self._KEY_TO_KIND[key]
        for value in removed:
            self._notify("remove", kind, value)
        return removed


def _append_unique(target: list[str], items: list[str]) -> list[str]:
    """Append items not already present (case-insensitive), return what was added."""
    existing = {x.lower() for x in target}
    added: list[str] = []
    for raw in items:
        value = raw.strip()
        if not value or value.lower() in existing:
            continue
        target.append(value)
        existing.add(value.lower())
        added.append(value)
    return added


def _remove_unique(target: list[str], items: list[str]) -> list[str]:
    """Remove items (case-insensitive) from target in place; return what was removed."""
    wanted = {x.lower() for x in items}
    removed = [value for value in target if value.lower() in wanted]
    target[:] = [value for value in target if value.lower() not in wanted]
    return removed
