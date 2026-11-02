"""Thread-safe token bucket.

Worst case in any window of length W seconds is capacity + rate * W requests, so a
budget of "N per W seconds" needs capacity + rate * W <= N.
"""

import threading

from nta.clock import Clock

_EPSILON = 1e-9    # tokens; at most one extra request per 10^9 acquires
_MIN_WAIT_S = 1e-6  # never ask the clock for a sleep shorter than this


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
                # Tolerance: float refill can land at 0.9999999999999, whose remaining wait
                # is too small to move the clock, which would spin forever.
                if self._tokens >= 1 - _EPSILON:
                    self._tokens -= 1
                    return
                wait = max((1 - self._tokens) / self.rate, _MIN_WAIT_S)
            self._clock.sleep(wait)


def sec_bucket(clock: Clock) -> TokenBucket:
    """SEC allows 10 req/s. 2 + 8/s keeps every 1-second window at <= 10."""
    return TokenBucket(rate=8.0, capacity=2.0, clock=clock)


def openfigi_bucket(clock: Clock, keyed: bool = False) -> TokenBucket:
    """OpenFIGI allows 25 requests per 60 s without a key, 250 per 60 s with one.
    Capacity 5 + 20 per window (60 s, or 6 s keyed) keeps any 60 s window at <= 25 / <= 205."""
    window = 6.0 if keyed else 60.0
    return TokenBucket(rate=20 / window, capacity=5.0, clock=clock)


def alpaca_bucket(clock: Clock) -> TokenBucket:
    """Alpaca free data plan allows 200 requests/min. 10 + 170/60 s keeps 60 s windows <= 180."""
    return TokenBucket(rate=170 / 60, capacity=10.0, clock=clock)
