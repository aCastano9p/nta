import random

import httpx
import pytest

from nta.http import send_with_retry
from nta.ratelimit import TokenBucket, openfigi_bucket, sec_bucket
from tests.fakes import FakeClock


def max_in_window(times: list[float], window: float) -> int:
    """Largest number of events in any closed window [t, t + window]."""
    times = sorted(times)
    best, j = 0, 0
    for i in range(len(times)):
        while times[i] - times[j] > window + 1e-9:
            j += 1
        best = max(best, i - j + 1)
    return best


def test_sec_bucket_never_exceeds_10_per_second_under_burst() -> None:
    clock = FakeClock()
    bucket = sec_bucket(clock)
    times = []
    for _ in range(200):
        bucket.acquire()
        times.append(clock.monotonic())
    assert max_in_window(times, 1.0) <= 10
    # ...and it is not pathologically slow: 200 requests in about 25 s at 8/s.
    assert times[-1] == pytest.approx((200 - 2) / 8, rel=0.01)


def test_sec_bucket_after_idle_still_capped() -> None:
    clock = FakeClock()
    bucket = sec_bucket(clock)
    times = []
    for burst in range(5):
        clock.advance(7.3)  # idle long enough to refill fully
        for _ in range(30):
            bucket.acquire()
            times.append(clock.monotonic())
    assert max_in_window(times, 1.0) <= 10


def test_openfigi_bucket_respects_25_per_minute() -> None:
    clock = FakeClock()
    bucket = openfigi_bucket(clock)
    times = []
    for _ in range(100):
        bucket.acquire()
        times.append(clock.monotonic())
    assert max_in_window(times, 60.0) <= 25


def test_bucket_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1, clock=FakeClock())


def _client(statuses: list[int], seen: list[float], clock: FakeClock,
            headers: dict[str, str] | None = None) -> httpx.Client:
    it = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(clock.monotonic())
        return httpx.Response(next(it), headers=headers or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retry_on_429_then_success() -> None:
    clock, seen = FakeClock(), []
    client = _client([429, 503, 200], seen, clock)
    req = client.build_request("GET", "https://www.sec.gov/x")
    resp = send_with_retry(client, req, bucket=sec_bucket(clock), clock=clock,
                           rng=random.Random(1))
    assert resp.status_code == 200
    assert len(seen) == 3


def test_backoff_never_exceeds_rate_budget() -> None:
    """Many concurrent-ish failing requests: every attempt still takes a bucket token."""
    clock, seen = FakeClock(), []
    client = _client([429] * 1000, seen, clock)
    bucket = sec_bucket(clock)
    rng = random.Random(7)
    for _ in range(20):
        req = client.build_request("GET", "https://www.sec.gov/x")
        resp = send_with_retry(client, req, bucket=bucket, clock=clock, rng=rng,
                               max_attempts=5, base_delay=0.001, max_delay=0.002)
        assert resp.status_code == 429  # gave up, returned last response
    assert len(seen) == 100
    assert max_in_window(seen, 1.0) <= 10


def test_retry_after_header_is_honored() -> None:
    clock, seen = FakeClock(), []
    client = _client([429, 200], seen, clock, headers={"Retry-After": "12"})
    req = client.build_request("GET", "https://api.openfigi.com/v3/mapping")
    send_with_retry(client, req, bucket=sec_bucket(clock), clock=clock, rng=random.Random(0))
    assert seen[1] - seen[0] >= 12


def test_non_retryable_status_returns_immediately() -> None:
    clock, seen = FakeClock(), []
    client = _client([404], seen, clock)
    req = client.build_request("GET", "https://www.sec.gov/x")
    assert send_with_retry(client, req, bucket=sec_bucket(clock), clock=clock).status_code == 404
    assert len(seen) == 1


def test_transport_error_retried_then_raised() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = client.build_request("GET", "https://www.sec.gov/x")
    with pytest.raises(httpx.ConnectError):
        send_with_retry(client, req, bucket=sec_bucket(clock), clock=clock, max_attempts=3)
    assert len(clock.sleeps) >= 2


class _GuardedClock(FakeClock):
    """Fails instead of hanging if acquire() spins without the clock moving."""

    def sleep(self, seconds: float) -> None:
        before = self.t
        super().sleep(seconds)
        if self.t == before:
            raise AssertionError(f"sleep({seconds!r}) did not advance the clock at t={before}")


@pytest.mark.parametrize("start", [0.0, 3.0, 1234.5678, 1e6, 1e7])
def test_bucket_terminates_despite_float_rounding(start: float) -> None:
    import random as _r
    rng = _r.Random(42)
    clock = _GuardedClock()
    clock.t = start
    bucket = sec_bucket(clock)
    times = []
    for _ in range(3000):
        if rng.random() < 0.05:
            clock.advance(rng.random() * 0.37)
        bucket.acquire()
        times.append(clock.monotonic())
    assert max_in_window(times, 1.0) <= 10
