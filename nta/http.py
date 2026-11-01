"""HTTP send with rate limiting, exponential backoff and full jitter.

Every attempt, including retries, takes a token from the bucket first, so backing off
can never push the request rate above the bucket's budget.
"""

import random
from email.utils import parsedate_to_datetime

import httpx

from nta.clock import Clock
from nta.logs import log
from nta.ratelimit import TokenBucket

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 300.0


def _retry_after_seconds(resp: httpx.Response, clock: Clock) -> float:
    value = resp.headers.get("Retry-After")
    if not value:
        return 0.0
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            return min(MAX_RETRY_AFTER, max(0.0, (when - clock.now()).total_seconds()))
        except (TypeError, ValueError):
            return 0.0


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**(attempt-1)))."""
    return rng.uniform(0, min(cap, base * 2 ** (attempt - 1)))


def send_with_retry(
    client: httpx.Client,
    request: httpx.Request,
    *,
    bucket: TokenBucket,
    clock: Clock,
    rng: random.Random | None = None,
    max_attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> httpx.Response:
    """Return the first non-retryable response, or the last response after max_attempts.
    Re-raises the last transport error if every attempt failed at the transport level."""
    rng = rng or random.Random()
    for attempt in range(1, max_attempts + 1):
        bucket.acquire()
        error: httpx.TransportError | None = None
        resp: httpx.Response | None = None
        try:
            resp = client.send(request)
        except httpx.TransportError as exc:
            error = exc
        if resp is not None and resp.status_code not in RETRY_STATUSES:
            return resp
        if attempt == max_attempts:
            if resp is not None:
                return resp
            assert error is not None
            raise error
        delay = backoff_delay(attempt, base_delay, max_delay, rng)
        if resp is not None:
            delay = max(delay, _retry_after_seconds(resp, clock))
        log("http_retry", url=str(request.url), attempt=attempt,
            status=resp.status_code if resp is not None else None,
            error=type(error).__name__ if error else None, sleep_s=round(delay, 3))
        clock.sleep(delay)
    raise AssertionError("unreachable")
