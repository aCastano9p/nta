"""Thread-safe token bucket.

Worst case in any window of length W seconds is capacity + rate * W requests, so a
budget of "N per W seconds" needs capacity + rate * W <= N.
"""

import threading

from nta.clock import Clock


class TokenBucket:
    def __init__(self, rate: float, capacity: float, clock: Clock) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._tokens = capacity
        self._last = clock.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            self._clock.sleep(wait)


def sec_bucket(clock: Clock) -> TokenBucket:
    """SEC allows 10 req/s. 2 + 8/s keeps every 1-second window at <= 10."""
    return TokenBucket(rate=8.0, capacity=2.0, clock=clock)


def openfigi_bucket(clock: Clock) -> TokenBucket:
    """OpenFIGI without a key allows 25 requests per 60 s. 5 + 20/60 s <= 25 per window."""
    return TokenBucket(rate=20 / 60, capacity=5.0, clock=clock)
