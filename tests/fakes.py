"""Test doubles."""

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Time only moves when someone sleeps (or the test calls advance)."""

    def __init__(self, start: datetime = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)) -> None:
        self._start = start
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self.t)
