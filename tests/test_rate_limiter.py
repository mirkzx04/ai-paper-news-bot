"""Tests for the shared Telegram rate limiter (per-chat + global caps).

Uses a fake monotonic clock and a fake sleeper that ADVANCES that clock, so we
assert the pacing deterministically without sleeping for real.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notify.rate_limiter import RateLimiter, TokenBucket  # noqa: E402


class _FakeClock:
    """A controllable monotonic clock whose ``sleep`` advances time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class TokenBucketTest(unittest.TestCase):
    def test_burst_then_paces_at_rate(self) -> None:
        clk = _FakeClock()
        # 2 tokens/s, capacity 2: two immediate consumes, the third waits 0.5s.
        tb = TokenBucket(rate=2.0, capacity=2.0, clock=clk.now, sleeper=clk.sleep)
        self.assertEqual(tb.consume(), 0.0)
        self.assertEqual(tb.consume(), 0.0)
        waited = tb.consume()
        self.assertAlmostEqual(waited, 0.5, places=6)

    def test_refill_over_time(self) -> None:
        clk = _FakeClock()
        tb = TokenBucket(rate=1.0, capacity=1.0, clock=clk.now, sleeper=clk.sleep)
        self.assertEqual(tb.consume(), 0.0)   # drains the one token
        clk.t += 1.0                           # a second passes -> 1 token back
        self.assertEqual(tb.consume(), 0.0)   # no wait needed


class RateLimiterTest(unittest.TestCase):
    def test_per_chat_interval_enforced_for_same_chat(self) -> None:
        clk = _FakeClock()
        rl = RateLimiter(global_rate=1000.0, per_chat_rate=1.0, global_burst=1000.0,
                         clock=clk.now, sleeper=clk.sleep)
        self.assertEqual(rl.acquire("chatA"), 0.0)     # first send, no wait
        waited = rl.acquire("chatA")                    # second send to same chat
        self.assertAlmostEqual(waited, 1.0, places=6)   # paced ~1 msg/s

    def test_distinct_chats_not_blocked_by_per_chat_limit(self) -> None:
        clk = _FakeClock()
        rl = RateLimiter(global_rate=1000.0, per_chat_rate=1.0, global_burst=1000.0,
                         clock=clk.now, sleeper=clk.sleep)
        self.assertEqual(rl.acquire("chatA"), 0.0)
        self.assertEqual(rl.acquire("chatB"), 0.0)      # different chat -> no wait

    def test_global_cap_applies_across_chats(self) -> None:
        clk = _FakeClock()
        # Global 2 msg/s, burst 2: the 3rd send across DIFFERENT chats must wait
        # on the shared global bucket even though per-chat is unhit.
        rl = RateLimiter(global_rate=2.0, per_chat_rate=1000.0, global_burst=2.0,
                         clock=clk.now, sleeper=clk.sleep)
        self.assertEqual(rl.acquire("a"), 0.0)
        self.assertEqual(rl.acquire("b"), 0.0)
        waited = rl.acquire("c")
        self.assertAlmostEqual(waited, 0.5, places=6)   # 1 token / 2 per s


if __name__ == "__main__":
    unittest.main()
