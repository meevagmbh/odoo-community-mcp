"""In-memory sliding-window rate limiter, keyed per API-key digest.

State lives in the process. With several replicas each one limits on its own,
so the effective limit is `replicas * per_minute`.
"""

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = per_minute
        self._clock = clock
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def tracked_keys(self) -> int:
        return len(self._hits)

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may make its next request (0 if it may now)."""
        window = self._hits.get(key)
        if self._limit <= 0 or not window or len(window) < self._limit:
            return 0
        return max(1, math.ceil(window[0] + WINDOW_SECONDS - self._clock()))

    def hit(self, key: str) -> int:
        """Record one request. Return 0 if allowed, else seconds until retry."""
        if self._limit <= 0:
            return 0
        now = self._clock()
        window = self._hits.pop(key, None) or deque()
        while window and window[0] <= now - WINDOW_SECONDS:
            window.popleft()
        self._hits[key] = window
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        if len(window) >= self._limit:
            return max(1, math.ceil(window[0] + WINDOW_SECONDS - now))
        window.append(now)
        return 0
