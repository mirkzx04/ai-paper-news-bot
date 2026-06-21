"""Tests for per-user flood control + input caps."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.flood_control import (  # noqa: E402
    MAX_REPORT_LEN,
    SlidingWindowRateLimiter,
    clamp_text,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FloodControlTest(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self) -> None:
        clk = _Clock()
        rl = SlidingWindowRateLimiter(max_events=3, window_seconds=60.0, clock=clk)
        self.assertEqual([rl.allow("u") for _ in range(4)], [True, True, True, False])

    def test_window_eviction_lets_user_through_again(self) -> None:
        clk = _Clock()
        rl = SlidingWindowRateLimiter(max_events=2, window_seconds=60.0, clock=clk)
        self.assertTrue(rl.allow("u"))
        self.assertTrue(rl.allow("u"))
        self.assertFalse(rl.allow("u"))      # over budget within the window
        clk.t = 61.0                          # window slides past the first events
        self.assertTrue(rl.allow("u"))

    def test_distinct_users_have_independent_budgets(self) -> None:
        rl = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, clock=_Clock())
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("b"))        # b unaffected by a
        self.assertFalse(rl.allow("a"))

    def test_none_key_is_never_limited(self) -> None:
        rl = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, clock=_Clock())
        self.assertTrue(rl.allow(None))
        self.assertTrue(rl.allow(None))

    def test_clamp_text(self) -> None:
        self.assertEqual(clamp_text("short", 10), "short")
        long = "x" * (MAX_REPORT_LEN + 50)
        out = clamp_text(long, MAX_REPORT_LEN)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), MAX_REPORT_LEN + 1)
        self.assertEqual(clamp_text(None, 5), "")


if __name__ == "__main__":
    unittest.main()
