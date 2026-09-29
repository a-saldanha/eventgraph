"""Simple in-memory rate limiting for /api/ask.

Two complementary limits:
  - Per-IP: sliding 1-minute window (token bucket).
  - Global: daily call cap across all IPs, resets at UTC midnight.

Both are in-process and reset on restart — appropriate for a single-process demo.
"""
from __future__ import annotations

import time
from calendar import timegm
from collections import defaultdict

_IP_CALLS_PER_MINUTE = 10
_GLOBAL_DAILY_CAP = 200


class RateLimiter:
    def __init__(
        self,
        calls_per_minute: int = _IP_CALLS_PER_MINUTE,
        global_daily_cap: int = _GLOBAL_DAILY_CAP,
    ):
        self._cpm = calls_per_minute
        self._daily_cap = global_daily_cap
        self._ip_calls: dict[str, list[float]] = defaultdict(list)
        self._global_day: float = self._today_start()
        self._global_count: int = 0

    @staticmethod
    def _today_start() -> float:
        t = time.gmtime()
        return float(timegm((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0)))

    def _reset_global_if_new_day(self) -> None:
        today = self._today_start()
        if today > self._global_day:
            self._global_day = today
            self._global_count = 0

    def check(self, ip: str) -> tuple[bool, str]:
        """Return (allowed, reason_msg). Call before serving /api/ask."""
        self._reset_global_if_new_day()
        if self._global_count >= self._daily_cap:
            return False, f"Global daily cap of {self._daily_cap} questions reached. Try again tomorrow."

        now = time.time()
        window = [t for t in self._ip_calls[ip] if now - t < 60]
        self._ip_calls[ip] = window
        if len(window) >= self._cpm:
            return False, f"Rate limit: at most {self._cpm} questions per minute per IP."

        return True, ""

    def record(self, ip: str) -> None:
        """Record a successful call (call after check returns True)."""
        self._ip_calls[ip].append(time.time())
        self._global_count += 1
