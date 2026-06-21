"""GDPR-style erasure — wipe everything the bot holds about one anonymous user.

A public service must let a user exercise the right to erasure (``/delete_me``).
A user is identified ONLY by their anonymous ``u_<hmac>`` id, so erasure means
removing every trace keyed by that id across the four stores:

  1. their per-user runtime dir ``data/users/<id>/`` (profile overlay, profile
     vector cache, feedback vector cache);
  2. their entry in the delivery registry ``data/user_registry.json`` (the
     encrypted chat id);
  3. their rows in the preference dataset ``data/preferences.jsonl`` — the future
     RankNet training data — by rewriting the file without that ``user_id``;
  4. (the caller also stops further digests by deleting/deactivating the registry
     entry — handled in 2.)

Everything here is defensive: a failure to erase one store is logged and erasure
continues with the others, and a missing store counts as "already erased".
``erase_user`` returns a summary dict so the command handler can report what was
removed. It NEVER raises toward the caller.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from src.store.profile_store import UserProfileStoreProvider

logger = logging.getLogger(__name__)


def _erase_user_dir(user_id: str, base_overlay_path: str) -> bool:
    """Delete ``data/users/<id>/`` (overlay + vector caches). True if it existed."""
    try:
        provider = UserProfileStoreProvider(base_overlay_path)
        # path_for returns .../users/<safe_id>/profile_overlay.json — the dir is
        # its parent. _safe_user_id sanitises the id (no path traversal).
        user_dir = Path(provider.path_for(user_id)).parent
        if user_dir.is_dir():
            shutil.rmtree(user_dir)
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — erasure must never crash the bot
        logger.warning("privacy: failed to erase user dir for %s: %s", user_id, exc)
        return False


def _erase_preference_rows(user_id: str, preferences_path: str) -> int:
    """Rewrite the JSONL dataset without rows for `user_id`. Returns rows removed.

    Reads every line, keeps only those whose ``user_id`` differs, and rewrites the
    file atomically (write a temp file, then replace). Malformed lines are kept
    verbatim (we never silently drop data we can't parse). NEVER raises.
    """
    path = Path(preferences_path)
    if not path.exists():
        return 0
    try:
        kept: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(stripped)  # keep unparseable lines untouched
                    continue
                if isinstance(record, dict) and record.get("user_id") == user_id:
                    removed += 1
                    continue
                kept.append(stripped)
        if removed:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for line in kept:
                    fh.write(line + "\n")
            tmp.replace(path)
        return removed
    except OSError as exc:
        logger.warning("privacy: failed to erase preference rows for %s: %s", user_id, exc)
        return 0


def erase_user(user_id: str, *, registry=None,
               base_overlay_path: str = "data/profile_overlay.json",
               preferences_path: str = "data/preferences.jsonl") -> dict:
    """Erase all data for `user_id` across every store. NEVER raises.

    Returns ``{"user_dir": bool, "registry": bool, "preference_rows": int}``
    describing what was actually removed, so the caller can confirm to the user.
    """
    summary = {"user_dir": False, "registry": False, "preference_rows": 0}
    if not user_id:
        return summary
    summary["user_dir"] = _erase_user_dir(user_id, base_overlay_path)
    if registry is not None:
        try:
            summary["registry"] = registry.delete(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("privacy: failed to erase registry entry for %s: %s", user_id, exc)
    summary["preference_rows"] = _erase_preference_rows(user_id, preferences_path)
    logger.info("privacy: erased user %s -> %s", user_id, summary)
    return summary
