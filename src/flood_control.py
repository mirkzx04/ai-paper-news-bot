"""Flood control + input caps for a PUBLIC bot.

Once anyone can message the bot, a single user (or a script) can hammer it with
commands, flood ``/report`` with junk, or send pathologically long inputs. None
of that should crash the poll or balloon the stores. This module provides two
cheap, dependency-free guards the poller applies per incoming update:

  * ``SlidingWindowRateLimiter`` — at most ``max_events`` actions per
    ``window_seconds`` per anonymous user id (a monotonic-clock sliding window).
    Over-limit actions are rejected (the poller ignores them, optionally with a
    single throttled "slow down" reply), never raising.
  * input caps — ``MAX_COMMAND_LEN`` / ``MAX_REPORT_LEN`` and ``clamp_text`` to
    bound the size of anything that gets stored or echoed.

Stdlib only; ``clock`` is injectable for deterministic tests.
"""

from __future__ import annotations

import time
from collections import deque

# Per-user incoming-command budget. Generous enough for real use, tight enough to
# stop a flood: ~20 actions / 60s.
_DEFAULT_MAX_EVENTS = 20
_DEFAULT_WINDOW_SECONDS = 60.0

# Hard caps on stored/echoed user input (Telegram messages can be up to 4096
# chars; we bound what we persist well under that).
MAX_COMMAND_LEN = 1000
MAX_REPORT_LEN = 2000


def clamp_text(text: str, limit: int) -> str:
    """Truncate `text` to `limit` chars (appending '…' when cut). Tolerates None."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


class SlidingWindowRateLimiter:
    """Per-key sliding-window limiter: ``max_events`` per ``window_seconds``.

    ``allow(key)`` records an action for `key` and returns True if it is within
    budget, False if the key has exceeded it in the current window. Old timestamps
    are evicted lazily on each call, so memory stays bounded by the number of
    recently-active keys. NEVER raises.
    """

    def __init__(self, max_events: int = _DEFAULT_MAX_EVENTS,
                 window_seconds: float = _DEFAULT_WINDOW_SECONDS,
                 *, clock=time.monotonic) -> None:
        self.max_events = int(max_events)
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._events: dict[str, deque] = {}

    def allow(self, key) -> bool:
        if key is None:
            return True  # can't rate-limit an unidentifiable sender; let it pass
        try:
            now = self._clock()
            cutoff = now - self.window_seconds
            bucket = self._events.get(key)
            if bucket is None:
                bucket = deque()
                self._events[key] = bucket
            # Evict timestamps older than the window.
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_events:
                return False
            bucket.append(now)
            return True
        except Exception:  # noqa: BLE001 — a limiter bug must never break the poll
            return True
