"""`PreferenceDataset` — append-only, timestamped log of user preference signals.

Unlike `ProfileStore` (which holds only the *current* configuration snapshot:
the live sets of authors/keywords/topics/...), this store keeps the **history**
of preference signals as a reusable, labelled-ish dataset. It is the unified
data backend the whole system writes preference signals into: explicit profile
edits, 👍/👎 votes, and sent-paper impressions.

Storage is **JSONL** (one compact JSON object per line) at `data/preferences.jsonl`.
JSONL is preferred over a single big JSON list because the dominant operation is
*append* and the file only grows: appending a line is O(1) and never rewrites
(or risks truncating) the whole file, and a single corrupt line costs at most one
event instead of the entire dataset.

Robustness mirrors `ReportLog` / `ErrorLog` / `ProfileStore`: a missing file is
an empty dataset, a corrupt/unreadable file (or a corrupt individual line) is
tolerated rather than fatal, the path is injectable via the constructor, and
**no method ever raises toward the caller** — the bot must never crash because a
preference signal could not be logged. Failures are reported through the module
logger only.

Event schema
------------
Every event is a JSON object. `log()` always stamps it with:

  - ``ts``   : event time, UTC ISO-8601 (``datetime.now(timezone.utc).isoformat()``)
  - ``type`` : the event kind (taken from the ``type`` key of the logged dict)
  - ``user_id`` *(optional)* : an anonymous ``u_<digest>`` id. Telegram nicknames,
    names, usernames and raw Telegram ids are not part of this schema.

The ``type`` values and their payloads:

``profile_add`` / ``profile_remove``  *(implemented now)*
    An explicit profile edit — the user added/removed an interest. This is an
    explicit preference signal.
        ``{"user_id": str, "kind": "author"|"keyword"|"topic"|"conference"|"seed", "value": str}``
    Emitted by `ProfileListener` (this module) wired to `ProfileStore`'s
    observer hook, one event per item actually added/removed.

``vote``  *(written by the 👍/👎 feedback loop; see TelegramPoller)*
    A 👍/👎 on a shown paper — the strongest, explicitly *labelled* signal.
        ``{"user_id": str, "signal": "up"|"down"|"none", "canonical_key": str,
           "score": float, "breakdown": dict, "text": str}``
    ``canonical_key`` identifies the paper, ``score`` is the ranker score it was
    shown with, ``breakdown`` is the per-scorer contribution dict, ``text`` is
    the paper text the user judged (title + abstract).
    ``signal`` is ``"up"`` / ``"down"`` for a vote, or ``"none"`` for a
    *toggle-off* — the user re-tapping their own current vote to withdraw it.
    The log is append-only, so a withdrawal is a fresh ``"none"`` event (not a
    deletion); consumers resolve the net state as the last event per key, where
    ``"none"`` means "no preference" (excluded from positives and negatives).

``impression``  *(written by TelegramNotifier)*
    A paper that was *shown* to the user (the exposure half of the vote signal,
    needed to model what was seen-but-not-voted).
        ``{"user_id": str, "canonical_key": str, "score": float,
           "breakdown": dict, "route": "alert"|"digest"}``

Profile edits, votes and impressions all write into this same store with the
same ``log()`` API.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class PreferenceDataset:
    def __init__(self, path: str = "data/preferences.jsonl", user_id: str | None = None) -> None:
        self.path = Path(path)
        self.user_id = user_id

    # ---- writing -----------------------------------------------------------
    def log(self, event: dict) -> None:
        """Append one event as a JSON line, stamping it with ``ts`` (UTC ISO-8601).

        The caller supplies the event body including its ``type``; ``log`` adds
        the timestamp. NEVER raises: any failure (disk error, non-serialisable
        payload, ...) is swallowed and reported through the module logger so a
        logging failure can never take down the bot.
        """
        try:
            # Stamp a fresh copy so we don't mutate the caller's dict.
            record = dict(event)
            if self.user_id is not None and "user_id" not in record:
                record["user_id"] = self.user_id
            record["ts"] = datetime.now(timezone.utc).isoformat()
            # `type` is part of the schema; default to "unknown" if omitted so a
            # malformed call still produces a readable, filterable line.
            record.setdefault("type", "unknown")

            line = json.dumps(record, ensure_ascii=False)

            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 — logging must never crash the bot
            logger.warning("failed to log preference event to %s: %s", self.path, exc)

    # ---- reading -----------------------------------------------------------
    def events(self, types: list[str] | None = None,
               user_id: str | None = None) -> list[dict]:
        """Return logged events, oldest first, optionally filtered by ``type``.

        A missing file yields ``[]``. Individual malformed/blank lines are
        skipped (and logged) rather than aborting the whole read, so one bad
        append can never hide the rest of the history. NEVER raises.
        """
        wanted = set(types) if types is not None else None
        wanted_user = user_id
        out: list[dict] = []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # A single corrupt line costs only that one event.
                        logger.warning("skipping corrupt line in %s", self.path)
                        continue
                    if not isinstance(record, dict):
                        continue
                    if wanted_user is not None and record.get("user_id") != wanted_user:
                        continue
                    if wanted is None or record.get("type") in wanted:
                        out.append(record)
        except FileNotFoundError:
            return out
        except OSError as exc:
            logger.warning("could not read preference dataset %s: %s", self.path, exc)
            return out
        return out

    def count(self) -> int:
        """Number of well-formed events in the dataset (0 if missing). NEVER raises."""
        return len(self.events())


class ProfileListener:
    """Observer that turns `ProfileStore` changes into ``PreferenceDataset`` events.

    This is the concrete listener that bridges the two stores while keeping them
    decoupled: `ProfileStore` only knows it holds an optional
    ``Callable[[str, str, str], None]`` (see its ``listener`` constructor arg);
    this class supplies that callable and translates ``(action, kind, value)``
    into a ``profile_add`` / ``profile_remove`` event.

    ``action`` is ``"add"`` or ``"remove"``; ``kind`` is one of
    ``author|keyword|topic|conference|seed``; ``value`` is the item text. It is
    designed to be passed as ``ProfileStore(..., listener=ProfileListener(ds))``.
    """

    _ACTION_TO_TYPE = {"add": "profile_add", "remove": "profile_remove"}

    def __init__(self, dataset: PreferenceDataset, user_id: str | None = None) -> None:
        self._dataset = dataset
        self.user_id = user_id

    def __call__(self, action: str, kind: str, value: str) -> None:
        # Tolerate an unexpected action label rather than dropping the signal.
        event_type = self._ACTION_TO_TYPE.get(action, f"profile_{action}")
        event = {"type": event_type, "kind": kind, "value": value}
        if self.user_id is not None:
            event["user_id"] = self.user_id
        self._dataset.log(event)
