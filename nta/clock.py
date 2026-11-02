"""Clock abstraction so rate limiting, backoff and scheduling are testable with a fake.

This is the only module that reads the wall clock or the monotonic clock directly (a test
enforces it). Code that makes decisions takes a Clock; the module-level helpers below are
for bookkeeping that never feeds a decision (log timestamps, ULIDs, signal handling,
CLI date defaults).
"""

import time
from datetime import UTC, date, datetime
from typing import Protocol


def wall_now() -> datetime:
    return datetime.now(UTC)


def monotonic() -> float:
    return time.monotonic()


def epoch_ms() -> int:
    return int(time.time() * 1000)


def local_today() -> date:
    return date.today()


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> datetime: ...  # timezone-aware UTC


class SystemClock:
    def monotonic(self) -> float:
        return monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def now(self) -> datetime:
        return wall_now()
