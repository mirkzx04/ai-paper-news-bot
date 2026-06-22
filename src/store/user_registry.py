"""UserRegistry — the delivery directory mapping anonymous ids -> chat ids.

PRIVACY DESIGN (read this first)
--------------------------------
The bot keeps two physically separate stores with deliberately different privacy
postures:

  * ``data/preferences.jsonl`` (PreferenceDataset) — the FUTURE RankNet training
    set. It holds ONLY an anonymous ``u_<hmac>`` id plus preference signals
    (votes, impressions, profile edits). It MUST NEVER contain a routable Telegram
    chat id, nickname, username, or display name.

  * ``data/user_registry.json`` (THIS module) — the delivery directory. To send a
    user their digest the bot needs a routable ``chat_id``, which is sensitive
    PII. It lives ONLY here, keyed by the same anonymous id, kept OUT of the
    training data, and **encrypted at rest** when a secret is configured.

So the README's "raw Telegram ids are not stored" holds for the *dataset*; the
*delivery registry* necessarily stores a routable id, but it is isolated and
encrypted, and never feeds model training.

Encryption
----------
Stdlib only (no ``cryptography`` dependency). When ``REGISTRY_SECRET`` (or, as a
fallback, ``USER_ID_SECRET``) is set, each ``chat_id`` is encrypted with an
HMAC-SHA256 keystream in counter mode (a standard PRF-CTR stream cipher) and
authenticated with an encrypt-then-MAC tag, with a fresh random nonce per record.
A wrong/rotated key fails the integrity check and the record is treated as
unreadable (the user is simply re-registered on their next interaction) rather
than crashing. With no secret the chat id is stored in clear text — acceptable
for local/dev, flagged for production by the secret-hardening checks in
``src.user_identity``.

Robustness mirrors ``PreferenceDataset`` / ``ProfileStore``: a missing file is an
empty registry, a corrupt file/record is tolerated, and **no method raises toward
the caller** — registration or delivery bookkeeping must never crash the bot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from src.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

# Canonical user statuses.
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"   # unsubscribed via /stop — keep data, stop digests
STATUS_BLOCKED = "blocked"     # bot can't reach the chat (403) — stop digests

_ENC_PREFIX = "enc:v1:"        # marks an encrypted chat_id blob in the JSON
_ENC_INFO = b"registry-chatid-enc-v1"   # domain separation for the derived key
_NONCE_LEN = 16
_TAG_LEN = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_secret() -> str | None:
    """The encryption secret, or None when none is configured (clear-text mode)."""
    return os.environ.get("REGISTRY_SECRET") or os.environ.get("USER_ID_SECRET") or None


def _derive_key(secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), _ENC_INFO, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 PRF in counter mode -> a keystream of `length` bytes."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _encrypt_chat_id(chat_id, secret: str) -> str:
    """Encrypt-then-MAC a chat id into ``enc:v1:<base64(nonce|ct|tag)>``."""
    key = _derive_key(secret)
    plaintext = str(chat_id).encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, _keystream(key, nonce, len(plaintext))))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_LEN]
    return _ENC_PREFIX + base64.b64encode(nonce + ciphertext + tag).decode("ascii")


def _decrypt_chat_id(blob: str, secret: str) -> str | None:
    """Reverse :func:`_encrypt_chat_id`; None on a bad tag (wrong/rotated key)."""
    try:
        raw = base64.b64decode(blob[len(_ENC_PREFIX):])
        if len(raw) < _NONCE_LEN + _TAG_LEN:
            return None
        nonce = raw[:_NONCE_LEN]
        tag = raw[-_TAG_LEN:]
        ciphertext = raw[_NONCE_LEN:-_TAG_LEN]
        key = _derive_key(secret)
        expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_LEN]
        if not hmac.compare_digest(tag, expected):
            return None  # wrong key / tampered: treat as unreadable
        plaintext = bytes(c ^ k for c, k in zip(ciphertext, _keystream(key, nonce, len(ciphertext))))
        return plaintext.decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — never raise toward the caller
        logger.warning("registry: failed to decrypt chat id: %s", exc)
        return None


class UserRegistry:
    """Persistent map ``u_<id>`` -> {chat_id, status, created_at, last_seen_at}.

    The chat id is encrypted at rest when a secret is configured. All methods are
    defensive: a load/save failure degrades to an empty/unchanged registry and is
    only logged, never raised.
    """

    def __init__(self, path: str = "data/user_registry.json", *,
                 secret: str | None = None) -> None:
        self.path = Path(path)
        # Resolve the secret once at construction; None => clear-text storage.
        self._secret = secret if secret is not None else _registry_secret()
        self._data: dict[str, dict] = {}
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            logger.warning("registry: could not read %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        # Atomic: a torn write would corrupt the encrypted chat-id directory,
        # which (unlike the vector caches) cannot be recomputed.
        try:
            atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except OSError as exc:
            logger.warning("registry: could not write %s: %s", self.path, exc)

    # ---- chat-id encoding helpers -----------------------------------------
    def _encode_chat_id(self, chat_id) -> str:
        if self._secret:
            return _encrypt_chat_id(chat_id, self._secret)
        return str(chat_id)

    def _decode_chat_id(self, stored) -> str | None:
        if not isinstance(stored, str):
            return None
        if stored.startswith(_ENC_PREFIX):
            if not self._secret:
                return None  # encrypted but no key available
            return _decrypt_chat_id(stored, self._secret)
        return stored  # clear-text (no-secret deployments)

    # ---- mutations ---------------------------------------------------------
    def register(self, user_id: str, chat_id) -> None:
        """Upsert a user: store/refresh their chat id and bump ``last_seen_at``.

        A user previously ``inactive``/``blocked`` is reactivated on a fresh
        interaction (they messaged the bot again). NEVER raises.
        """
        if not user_id or chat_id is None:
            return
        try:
            rec = self._data.get(user_id, {})
            now = _now_iso()
            rec["chat_id"] = self._encode_chat_id(chat_id)
            rec["status"] = STATUS_ACTIVE
            rec.setdefault("created_at", now)
            rec["last_seen_at"] = now
            self._data[user_id] = rec
            self._save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("registry: register failed for %s: %s", user_id, exc)

    def set_status(self, user_id: str, status: str) -> None:
        """Set a user's status (e.g. STATUS_INACTIVE on /stop, STATUS_BLOCKED on 403)."""
        if user_id not in self._data:
            return
        try:
            self._data[user_id]["status"] = status
            self._save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("registry: set_status failed for %s: %s", user_id, exc)

    def delete(self, user_id: str) -> bool:
        """Remove a user entirely (used by /delete_me). True if a row was removed."""
        if user_id not in self._data:
            return False
        try:
            del self._data[user_id]
            self._save()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("registry: delete failed for %s: %s", user_id, exc)
            return False

    # ---- reads -------------------------------------------------------------
    def get(self, user_id: str) -> dict | None:
        """The decoded record (chat_id in clear) for delivery, or None.

        Returns a COPY with ``chat_id`` decrypted; None if unknown or the chat id
        can't be decrypted (wrong/rotated key — the user re-registers next time).
        """
        rec = self._data.get(user_id)
        if rec is None:
            return None
        chat_id = self._decode_chat_id(rec.get("chat_id"))
        if chat_id is None:
            return None
        out = dict(rec)
        out["chat_id"] = chat_id
        return out

    def is_active(self, user_id: str) -> bool:
        rec = self._data.get(user_id)
        return rec is not None and rec.get("status", STATUS_ACTIVE) == STATUS_ACTIVE

    def active_users(self) -> list[dict]:
        """All deliverable users as ``{"user_id", "chat_id", ...}`` (decoded).

        Skips non-active users and any whose chat id can't be decoded. Order is
        insertion order (stable for a deterministic fan-out).
        """
        out: list[dict] = []
        for user_id, rec in self._data.items():
            if rec.get("status", STATUS_ACTIVE) != STATUS_ACTIVE:
                continue
            chat_id = self._decode_chat_id(rec.get("chat_id"))
            if chat_id is None:
                continue
            entry = dict(rec)
            entry["user_id"] = user_id
            entry["chat_id"] = chat_id
            out.append(entry)
        return out

    def count(self) -> int:
        return len(self._data)
