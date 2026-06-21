"""Shared Telegram rate limiter — respects BOTH the per-chat and global caps.

Telegram enforces two independent throttles on the Bot API:
  * per-chat — roughly 1 message/second to any one chat;
  * global   — roughly 30 messages/second across all chats a bot talks to.

The original notifier honoured only the per-chat limit via a blunt
``time.sleep(1.0)``. A per-user digest fan-out (N users x M papers) must also
stay under the global ~30 msg/s ceiling, shared across all per-user notifiers in
one run — so ONE ``RateLimiter`` instance is created per digest run and passed to
every per-user ``TelegramNotifier``. Per-chat pacing still applies per chat_id;
the global budget is enforced across users. The notifier's reactive 429
``retry_after`` handling stays as the backstop.

Stdlib only (``threading`` + ``time``). ``clock`` / ``sleeper`` are injectable so
tests can drive deterministic, instant waits without real sleeping.
"""

from __future__ import annotations

import threading
import time

_DEFAULT_GLOBAL_RATE = 25.0   # < the ~30 msg/s global ceiling, with headroom
_DEFAULT_PER_CHAT_RATE = 1.0  # the ~1 msg/s per-chat ceiling
_DEFAULT_GLOBAL_BURST = 5.0   # allow a small burst before steady-state pacing


class TokenBucket:
    """Monotonic-clock token bucket: ``rate`` tokens/s, capped at ``capacity``.

    Thread-safe. ``consume`` blocks (via the injected ``sleeper``) until enough
    tokens have accrued, then debits them; it returns the total seconds it slept
    so callers/tests can assert the pacing.
    """

    def __init__(self, rate, capacity=None, *, clock=time.monotonic, sleeper=time.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._tokens = self.capacity
        self._last = self._clock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def consume(self, tokens: float = 1.0) -> float:
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            # Sleep OUTSIDE the lock so other chats aren't blocked while we wait.
            self._sleeper(sleep_for)
            waited += sleep_for


class RateLimiter:
    """Composite per-chat + global limiter; share one instance across a digest run.

    ``acquire(chat_id)`` blocks until BOTH the per-chat minimum interval AND the
    global token bucket allow a send, then returns the seconds waited. Distinct
    chats do not block each other on the per-chat limit (only on the shared
    global bucket), so a fan-out can proceed near the global ceiling while still
    pacing each individual chat to ~1 msg/s.
    """

    def __init__(self, *, global_rate=_DEFAULT_GLOBAL_RATE,
                 per_chat_rate=_DEFAULT_PER_CHAT_RATE,
                 global_burst=_DEFAULT_GLOBAL_BURST,
                 clock=time.monotonic, sleeper=time.sleep):
        if per_chat_rate <= 0:
            raise ValueError("per_chat_rate must be > 0")
        self._clock = clock
        self._sleeper = sleeper
        self._global = TokenBucket(global_rate, global_burst, clock=clock, sleeper=sleeper)
        self._per_chat_interval = 1.0 / float(per_chat_rate)
        self._last_per_chat: dict[str, float] = {}
        self._chat_lock = threading.Lock()

    def _per_chat_wait_locked(self, chat_id: str, now: float) -> float:
        last = self._last_per_chat.get(chat_id)
        wait = 0.0 if last is None else max(0.0, self._per_chat_interval - (now - last))
        # Reserve this chat's next slot so concurrent callers for the SAME chat
        # serialise correctly even before the sleep below completes.
        self._last_per_chat[chat_id] = now + wait
        return wait

    def acquire(self, chat_id) -> float:
        with self._chat_lock:
            now = self._clock()
            per_chat_wait = self._per_chat_wait_locked(str(chat_id), now)
        if per_chat_wait > 0:
            self._sleeper(per_chat_wait)
        global_wait = self._global.consume(1.0)
        return per_chat_wait + global_wait
