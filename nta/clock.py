"""Clock abstraction so rate limiting, backoff and scheduling are testable with a fake."""

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> datetime: ...  # timezone-aware UTC


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now(UTC)
