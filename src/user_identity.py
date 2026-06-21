"""Anonymous user identifiers for multi-user Telegram state.

The bot must be able to keep separate profiles and preference samples per user
without storing Telegram nicknames, first names, or raw Telegram ids. We derive a
stable pseudonymous id with HMAC-SHA256 over Telegram's numeric ``from.id``.
Only the resulting ``u_<digest>`` value is persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

# The dev fallback the secret chain lands on when nothing is configured. Used by
# the hardening checks below to detect an unconfigured deployment.
_DEV_FALLBACK_SECRET = "dev-only-user-id-secret"
# A configured secret should be reasonably long and high-entropy. We can't measure
# true entropy cheaply, but we can reject the obvious failure modes: too short, or
# the dev default. 24 chars is a sane floor for a random secret.
_MIN_SECRET_LEN = 24


def _secret(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("USER_ID_SECRET")
        or os.environ.get("TELEGRAM_USER_ID_SALT")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or _DEV_FALLBACK_SECRET
    )


class WeakUserIdSecretError(RuntimeError):
    """Raised by :func:`assert_strong_secret` when a strong secret is required but
    the configured ``USER_ID_SECRET`` is missing, the dev default, or too short."""


def secret_is_strong(secret: str | None = None) -> bool:
    """True if a dedicated, sufficiently long ``USER_ID_SECRET`` is configured.

    Strong means: ``USER_ID_SECRET`` (or ``TELEGRAM_USER_ID_SALT``) is set, is at
    least ``_MIN_SECRET_LEN`` chars, and is not the dev default. Falling back to
    the bot token or the dev string is treated as NOT strong — for a public
    deployment the anonymisation key must be a dedicated, stable, random value.
    """
    value = (
        secret
        or os.environ.get("USER_ID_SECRET")
        or os.environ.get("TELEGRAM_USER_ID_SALT")
    )
    if not value:
        return False
    return value != _DEV_FALLBACK_SECRET and len(value) >= _MIN_SECRET_LEN


def assert_strong_secret(*, require: bool | None = None) -> None:
    """Enforce a strong ``USER_ID_SECRET`` in production; warn otherwise.

    ``require`` decides the policy:
      * ``True``  -> raise :class:`WeakUserIdSecretError` when the secret is weak;
      * ``False`` -> only log a warning;
      * ``None``  -> auto-detect: required when ``BOT_ENV=production`` or
        ``REQUIRE_USER_ID_SECRET`` is truthy, else warn-only.

    This keeps local/dev frictionless while making a public deployment fail fast on
    a missing/weak anonymisation key (which would make ``u_<id>`` values guessable
    or unstable). NEVER raises unless the policy is "require".
    """
    if require is None:
        env = (os.environ.get("BOT_ENV") or "").strip().lower()
        flag = (os.environ.get("REQUIRE_USER_ID_SECRET") or "").strip().lower()
        require = env == "production" or flag in ("1", "true", "yes", "on")
    if secret_is_strong():
        return
    msg = ("USER_ID_SECRET is missing, too short (<%d chars), or the dev default; "
           "set a long random USER_ID_SECRET for a public deployment "
           "(it keeps anonymous user ids stable and unguessable)." % _MIN_SECRET_LEN)
    if require:
        raise WeakUserIdSecretError(msg)
    logger.warning("%s", msg)


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
