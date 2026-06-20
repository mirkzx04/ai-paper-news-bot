"""Anonymous user identifiers for multi-user Telegram state.

The bot must be able to keep separate profiles and preference samples per user
without storing Telegram nicknames, first names, or raw Telegram ids. We derive a
stable pseudonymous id with HMAC-SHA256 over Telegram's numeric ``from.id``.
Only the resulting ``u_<digest>`` value is persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import os


def _secret(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("USER_ID_SECRET")
        or os.environ.get("TELEGRAM_USER_ID_SALT")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or "dev-only-user-id-secret"
    )


def anonymous_user_id(raw_user_id, *, secret: str | None = None) -> str | None:
    """Return a stable anonymous id for a Telegram user id, or None if missing."""
    if raw_user_id is None:
        return None
    msg = str(raw_user_id).encode("utf-8")
    key = _secret(secret).encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).hexdigest()[:24]
    return f"u_{digest}"


def telegram_user_id(payload: dict | None) -> str | None:
    """Extract an anonymous user id from a Telegram ``from`` object."""
    if not isinstance(payload, dict):
        return None
    return anonymous_user_id(payload.get("id"))
