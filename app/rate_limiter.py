import time
from collections import deque

from app.config import SEND_RATE_LIMIT, SEND_RATE_WINDOW_SECONDS


class SlidingWindowLimiter:
    """Mirrors the mock API's own limit (10 req / rolling 60s) so we back
    off *before* hitting 429s instead of just reacting to them. We still
    handle 429s gracefully (see worker.py) since the two clocks won't be
    perfectly in sync, but this keeps us from hammering the API and
    wasting attempts."""

    def __init__(self, limit: int = SEND_RATE_LIMIT, window: float = SEND_RATE_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self._timestamps: deque = deque()

    def try_acquire(self) -> bool:
        now = time.time()
        while self._timestamps and now - self._timestamps[0] >= self.window:
            self._timestamps.popleft()
        if len(self._timestamps) < self.limit:
            self._timestamps.append(now)
            return True
        return False


limiter = SlidingWindowLimiter()
